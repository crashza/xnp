#!/bin/python

#
# Script responsible for downloading and 
# importing GNP data
#
# Script should be run as many times as needed 
# if process has completed just skip process
#
# Author Trevor Steyn <trevor@webon.co.za>
#

import re
import os
import time
import json
import gzip
import ftplib
import tarfile
import logging
import argparse
import datetime
from config import *
import mysql.connector
from ftplib import FTP_TLS
import xml.etree.cElementTree as ET
from xml.etree.cElementTree import iterparse


############################### Start of Defs ##########################################

def download_npc():
    ftp_files   = []
    os_files    = []
    try:
        ftps         = FTP_TLS()
        ftps.connect(CFG_FTPS_HOST,CFG_FTPS_PORT)
        log_it('connected to ' + CFG_FTPS_HOST + ' welcome message: ' + str(ftps.getwelcome()), 'info')  
        ftps.login(CFG_FTPS_USER,CFG_FTPS_PASS)
        ftps.prot_p()
        log_it('changing dir: ' + CFG_FTPS_DIR , 'info')
        ftps.cwd(CFG_FTPS_DIR)
        ftp_files = ftps.nlst()
        for f in ftp_files:
            if not os.path.isfile(CFG_ARCHIVE_DIR + f):
                ftps.retrbinary('RETR ' + f, open(CFG_ARCHIVE_DIR + f , 'wb').write)
                log_it('downloading file ' + f , 'info')
            else:
                log_it('skipping ' + f + ' as it already exists in ' + CFG_ARCHIVE_DIR, 'debug')
    except ftplib.all_errors as e:
        log_it('unable to connect to ' + CFG_FTPS_HOST + ' %s' %e, 'error')

# Loggin definition

def log_it(msg,level):
    # To log to only std out use level debug
    if args.debug:
        print (level + ' ' + msg)
    if level == 'info':
        logging.info(msg)
    elif level == 'warning':
        logging.warning(msg)
    elif level == 'error':
        logging.error(msg)
        exit(1)

# Create dict of routing labels

def get_routing_labels():
    try:
        query = ('''SELECT i_routing_label, routing_label 
                    FROM routing_labels ''')
        cursor.execute(query)
        routing_labels = {}
        for (i_routing_label, routing_label) in cursor:
            routing_labels[routing_label] = i_routing_label
        return routing_labels
    except mysql.connector.Error as err:
        log_it('Mysql Error while getting routing labels: ' + str(err), 'error')

# Get home prefixes and return dict

def get_home_prefixes():
    try:
        query = ('''SELECT a.prefix, b.routing_label 
                    FROM home_prefixes a 
                    INNER JOIN routing_labels b 
                    ON a.i_routing_label = b.i_routing_label''')
        cursor.execute(query)
        home_prefixes = {}
        for (prefix, routing_label) in cursor:
            home_prefixes[prefix] = routing_label
        return home_prefixes
    except mysql.connector.Error as err:
            log_it('Mysql Error while getting home networks: ' + str(err), 'error')

# Finding best prefix match

def find_best_match(string,prefixes):
    lookup = string
    while len(lookup) > 0:
        if lookup in prefixes.keys():
            print("%s %s %s" %(string,lookup,prefixes[lookup])) 
            best_match ={'prefix':lookup,'ro_label':prefixes[lookup]}
            return best_match
        lookup = lookup[:-1]
    log_it('no home network found for ' + string + ' please update databse','error')

# Insert portings to DB porting

def insert_portings_db(values):
    #print str(len(values))
    try:
        q = ''' INSERT INTO portings
                (destination,i_routing_label,port_id,action,file)
                VALUES(%s,%s,%s,%s,%s) 
                ON DUPLICATE KEY UPDATE 
                i_routing_label=VALUES(i_routing_label),
                port_id=VALUES(port_id),
                action=VALUES(action),
                file=VALUES(file)
            '''
        cursor.executemany(q,values)
    except mysql.connector.Error as err:
        log_it('Mysql Error while inserting into portings: ' + str(err), 'error')

# Delete portings to DB porting

def delete_portings_db(values):
    for destination in values:
        try:
            q = 'DELETE FROM portings WHERE destination = \'' + destination + '\''
            cursor.execute(q)
        except mysql.connector.Error as err:
            log_it('Mysql Error when deleting from portings: ' + str(err), 'error')

        if cursor.rowcount  == 0:
            log_it('destination:' + destination + ' not found in portings while trying to delete possible stale DB','warning')

def save_lf_processed(values):
    for p_file in values:
        try:
            q = 'INSERT INTO processed_files (file) VALUES(\'' + p_file + '\')'
            cursor.execute(q)
            log_it('inserting file ' + p_file + ' into processed_files table','info')
        except mysql.connector.Error as err:
            log_it('Mysql Error when inserting into files: ' + str(err), 'error')


############################### End of Defs ##########################################

# Config vars this can overwrite config.py

CFG_ARCHIVE_DIR = '/srv/files/gnp/'
CFG_DB_NAME     = 'gnp' 
CFG_TMP_XML     = '/tmp/gnp.xml'
CFG_DB_MAX_INS  = 500000

# parse CLI arguments

parser = argparse.ArgumentParser(description='MNP import script');
parser.add_argument('-f', '--file', type=str, help='Manual file to import')
parser.add_argument('-d', '--debug',action='store_true', help='Print debug info')
args = parser.parse_args()

# Setup Logging

logging_format = '%(asctime)-15s %(levelname)s  %(message)s'
logging.basicConfig(level=logging.INFO,format=logging_format,filename='/var/log/gnp/gnp.log')

download_npc()

# Check last file downloaded

# Connect to DB use this connection for the rest of the script

try:
    cnx =  mysql.connector.connect(host=CFG_DB_HOST, user=CFG_DB_USER, password=CFG_DB_PASS, database=CFG_DB_NAME)
    cursor = cnx.cursor()
    query = ("SELECT file from processed_files order by i_file DESC LIMIT 1")
    cursor.execute(query)
    last_file = cursor.fetchone()
except mysql.connector.Error as err:
    log_it("DB: {}".format(err), 'error')

# Slurp up all files starting with DCRDBDDownload

npc_files = []
for f in os.listdir(CFG_ARCHIVE_DIR) : 
    if f.startswith('DGNPDownload'):
        npc_files.append(f)

npc_files = sorted(npc_files)
if args.file:
    process_files = [args.file]
    log_it('manual file selected ' + args.file, 'info')
elif last_file:
    last_file = last_file[0]
    try:
        npc_index = npc_files.index(last_file)
    except:
        log_it('last file processed not found in archive ' + last_file, 'error')
    npc_index = npc_files.index(last_file)
    process_files = npc_files[npc_index + 1:]
    log_it('last file processed was ' + last_file, 'info')
else:
    process_files = npc_files
    log_it('no processed files found', 'info')
if not process_files:
    log_it('no new files to process exiting....','info')
    exit()

# We now have a list of files that need to be processed

ported_numbers = {}

# We process files in order of date
# newer files will overwrite ported numbers dict

for f in process_files:
    log_it('processing file ' + f, 'info')
    text_file = open(CFG_TMP_XML, "w")
    myxml = gzip.open(CFG_ARCHIVE_DIR + '/' + f, 'rt') 
    text_file.write(myxml.read())
    text_file.close()
    myxml = ''
    log_it('decompressed XML into memory for ' + f, 'info')
    for event,elem in iterparse(CFG_TMP_XML):
        if elem.tag == "ActivatedNumber":
            num_range   = elem.find('DNRanges')
            id_number   = elem.find('IDNumber').text
            ro_label    = elem.find('RNORoute').text
            action      = elem.find('Action').text
            range_start = num_range.find('DNFrom').text
            range_end   = num_range.find('DNTo').text
            temp_count  = int(range_start)
            while temp_count <= int(range_end):
                msisdn                              = str(temp_count)
                ported_numbers[msisdn]              = {}
                ported_numbers[msisdn]['id_number'] = id_number
                ported_numbers[msisdn]['ro_label']  = ro_label
                ported_numbers[msisdn]['action']    = action
                ported_numbers[msisdn]['file']      = f
                ported_numbers
                temp_count = temp_count + 1
            elem.clear()

    log_it('XML schema loaded into memory for ' + f, 'info')
    os.remove(CFG_TMP_XML)

# Read info about routing labels and home networks

routing_labels = get_routing_labels()
home_prefixes = get_home_prefixes()

insert_db = []
delete_db = []

# Lets Generate the queries to be inserted into DB

port_count = 0
unport_count = 0

for number in ported_numbers.keys():
    #print 'looking up ' + str(number) + ' for best match'
    #print port_count
    best_match = find_best_match(number,home_prefixes)
    if ported_numbers[number]['ro_label'] == best_match['ro_label']:
        delete_db.append(number)
        unport_count = unport_count + 1
        # Free up memory
        #TODO Free up memory
        #del ported_numbers[number]
        #print number + ' ' + best_match['ro_label']
    else:
        port_count = port_count + 1
        insert_db.append((number,routing_labels[ported_numbers[number]['ro_label']],ported_numbers[number]['id_number'],ported_numbers[number]['action'],ported_numbers[number]['file']))
        # Free up memory
        #TODO Free up memory
        #del ported_numbers[number]

# Fix for huge table like full tables

if ( (len(insert_db) - 1 ) > CFG_DB_MAX_INS ):
    log_it('huge data input detected splitting up as per limit of ' + str(CFG_DB_MAX_INS), 'info') 
    counter = CFG_DB_MAX_INS
    last_counter = 0
    while last_counter != (len(insert_db) -1): 
        log_it('query splitting up from ' + str(last_counter) + ' to ' + str(counter),'info')
        if counter >= (len(insert_db) -1):
            insert_portings_db(insert_db[last_counter:])
            last_counter = ( len(insert_db) - 1 )
        else:
            insert_portings_db(insert_db[last_counter:counter])
            last_counter = counter
        counter = counter + CFG_DB_MAX_INS
        
else:
    insert_portings_db(insert_db)

# Delete if porting back to home

delete_portings_db(delete_db)
    
# Save last file processed

save_lf_processed(process_files)

cnx.commit()
cnx.close()

log_it('GNP process completed Portings:' + str(port_count) + ' PortBack:' + str(unport_count) + ' Total:' + str(port_count + unport_count), 'info') 
