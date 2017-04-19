__author__ = 'matiasbevilacqua'

from settings import logger_getDebugMode
import logging
from mpEngineProdCons import MPEngineProdCons
from mpEngineWorker import MPEngineWorker
import Queue
import os
import re
import sqlite3
import ntpath
from contextlib import closing
import time
import struct
import hashlib
from appAux import update_progress, chunks, loadFile, psutil_phymem_usage, file_size
import appDB
import settings
from ShimCacheParser import read_mir, write_it
from AmCacheParser import _processAmCacheFile_StringIO
import zipfile
import contextlib
import xml.etree.ElementTree as ET
from datetime import timedelta, datetime
import sys
import traceback
import signal
import gc
import cProfile
from Ingest import appcompat_parsed
from Ingest import appcompat_mirregistryaudit
from Ingest import amcache_miracquisition
from Ingest import appcompat_mirlua_v1
from Ingest import appcompat_mirlua_v2
from Ingest import amcache_mirlua_v1
from Ingest import appcompat_csv
from Ingest import appcompat_redline
from Ingest import appcompat_raw_hive
from Ingest import appcompat_miracquisition
from Ingest import amcache_raw_hive
try:
    import pyregf
except ImportError:
    if settings.__PYREGF__:
        settings.__PYREGF__ = False
        print "Ooops seems you don't have pyregf!"
        print "AmCache loading support will be disabled"
else: settings.__PYREGF__ = True


logger = logging.getLogger(__name__)
_tasksPerJob = 10
supported_ingest_plugins = ['appcompat_parsed.Appcompat_parsed', 'amcache_miracquisition.Amcache_miracquisition',
                            'appcompat_mirregistryaudit.Appcompat_mirregistryaudit', 'amcache_mirlua_v1.Amcache_mirlua_v1',
                            'appcompat_mirlua_v2.Appcompat_mirlua_v2', 'appcompat_csv.Appcompat_csv',
                            'appcompat_redline.Appcompat_redline', 'appcompat_raw_hive.Appcompat_Raw_hive',
                            'appcompat_miracquisition.Appcompat_miracquisition', 'amcache_raw_hive.Amcache_Raw_hive']

# Load IngestTypes
ingest_plugins = {}
ingest_plugins_types_stack = []
for plugin in supported_ingest_plugins:
    ingest_plugins[eval(plugin).ingest_type] = eval(plugin)()
    ingest_plugins_types_stack.append(eval(plugin).ingest_type)


def do_cprofile(func):
    def profiled_func(*args, **kwargs):
        profile = cProfile.Profile()
        try:
            profile.enable()
            result = func(*args, **kwargs)
            profile.disable()
            return result
        finally:
            profile.print_stats()
    return profiled_func


class appLoadProd(MPEngineWorker):

    def do_work(self, next_task):
        self.logger.debug("do_work")
        rowsData = next_task()

        # Sanityzing entries
        for x in rowsData:
            # Check if we've been killed
            self.check_killed()
            sanityCheckOK = True
            # todo: Bring back here sanity check on AMCache dates (as the SQLite driver would die later when querying invalid dates)
            try:
                if sanityCheckOK:
                    # todo: Maybe we don't need this after the ISO patch to ShimCacheParser?
                    if x.LastModified != "N/A" and x.LastModified != None:
                        x.LastModified = datetime.strptime(x.LastModified, "%Y-%m-%d %H:%M:%S")
                    else:
                        x.LastModified = datetime.min

                    if x.LastUpdate != "N/A" and x.LastUpdate != None:
                        x.LastUpdate = datetime.strptime(x.LastUpdate, "%Y-%m-%d %H:%M:%S")
                    else:
                        x.LastUpdate = datetime.min

                    # We use FirstRun as LastModified for AmCache entries
                    if x.EntryType == settings.__AMCACHE__:
                        x.LastModified = x.FirstRun

                    # We use Modified2 as LastUpdate for AmCache entries
                    if x.EntryType == settings.__AMCACHE__:
                        x.LastUpdate = x.Modified2

                    # Sanitize things up (AmCache is full of these 'empty' entries which I don't have a clue what they are yet)
                    if x.FilePath is None:
                        x.FilePath = "None"
                    else:
                        x.FilePath = x.FilePath.replace("'", "''")
                        # Trim out UNC path prefix
                        x.FilePath = x.FilePath.replace("\\??\\", "")
                        # Trim out SYSVOL path prefix
                        x.FilePath = x.FilePath.replace("SYSVOL", "C:")
                    if x.FileName is None:
                        x.FileName = "None"
                    else:
                        x.FileName = x.FileName.replace("'", "''")
                else:
                    rowsData.remove(x)
            except Exception as e:
                self.logger.warning("Exception processing row (%s): %s" % (e.message, x))
                sanityCheckOK = False
                pass
        return rowsData


class appLoadCons(MPEngineWorker):

    def run(self):
        # Note: __init__ runs on multiprocessing's main thread and as such we can't use that to init a sqlite connection
        assert(len(self.extra_arg_list) == 1)
        self.dbfilenameFullPath = self.extra_arg_list[0]
        self.DB = None
        self.conn = None

        # Init DB access to DB
        self.DB = appDB.DBClass(self.dbfilenameFullPath, True, settings.__version__)
        # self.DB.appInitDB()
        self.conn = self.DB.appConnectDB()

        # Call super run to continue with the natural worker flow
        super(appLoadCons, self).run()

        # Close DB connection
        self.logger.debug("%s - closing down DB" % self.proc_name)
        self.conn.close()
        del self.DB


    def do_work(self, entries_fields_list):
        self.logger.debug("do_work")
        number_of_grabbed_tasks = 1
        min_bucket = _tasksPerJob * 5
        max_bucket = _tasksPerJob * 10
        bucket_ready = False

        if entries_fields_list:
            numFields = len(entries_fields_list[0]._asdict().keys()) - 4
            valuesQuery = "(NULL," + "?," * numFields + "0, 0)"
            try:
                insertList = []
                with closing(self.conn.cursor()) as c:
                    # Grab a bunch of results to reduce # of DB commits
                    while not bucket_ready:
                        try:
                            self.logger.debug("%s - trying to grab additional task" % self.proc_name)
                            tmp = self.task_queue.get_nowait()
                            number_of_grabbed_tasks += 1
                            self.update_progress()
                            entries_fields_list.extend(tmp)
                        except Queue.Empty:
                            # If we're over min_bucket we can proceed
                            if number_of_grabbed_tasks > min_bucket:
                                logger.debug("%s - Over min_bucket" % self.proc_name)
                                bucket_ready = True
                            else:
                                # Grab tasks and progress
                                with self.available_task_num.get_lock():
                                    available_task_num = self.available_task_num.value
                                with self.progress_counter.get_lock():
                                    progress_counter = self.progress_counter.value
                                # If we just have to wait to get enough tasks to fill our bucket we wait
                                if self.total_task_num - progress_counter > min_bucket:
                                    self.logger.debug("%s - waiting for bucket to be filled (%d/%d), sleeping" %
                                                        (self.proc_name, number_of_grabbed_tasks, min_bucket))
                                    time.sleep(1)
                                else:
                                    self.logger.debug("%s - Going for the last bucket!" % self.proc_name)
                                    bucket_ready = True

                        #If we've reached max_bucket we move on to consume it
                        if number_of_grabbed_tasks > max_bucket:
                            bucket_ready = True

                    for x in entries_fields_list:
                        # Ugly hack as some versions of libregf seems to return utf16 for some odd reason
                        # Does not work as some stuff will decode correctly when it's not really UTF16, need to find root cause to decode when required only.
                        # if x.FilePath is not None:
                        #     try:
                        #         tmp_file_path = (x.FilePath).decode('utf-16')
                        #         # print "string is UTF-8, length %d bytes" % len(string)
                        #     except UnicodeError:
                        #         tmp_file_path = x.FilePath
                        #         # print "string is not UTF-8"

                        tmp_file_path = x.FilePath
                        # Add FilePath if not there yet
                        c.execute("INSERT OR IGNORE INTO FilePaths VALUES (NULL, '%s')" % tmp_file_path)
                        # Get assigned FilePathID
                        x.FilePathID = self.DB.QueryInt("SELECT FilePathID FROM FilePaths WHERE FilePath = '%s'" % tmp_file_path)

                        # Append the record to our insertList
                        # Note: Info from AmCache is already in datetime format
                        insertList.append((x.HostID, x.EntryType, x.RowNumber, x.LastModified, x.LastUpdate, x.FilePathID, \
                                           x.FileName, x.Size, x.ExecFlag, x.SHA1, x.FileDescription, x.FirstRun, x.Created, \
                                           x.Modified1, x.Modified2, x.LinkerTS, x.Product, x.Company, x.PE_sizeofimage, \
                                           x.Version_number, x.Version, x.Language, x.Header_hash, x.PE_checksum, str(x.SwitchBackContext), x.InstanceID))

                    # self.logger.debug("%s - Dumping result set into database %d rows / %d tasks" % (self.proc_name, len(insertList), number_of_grabbed_tasks))
                    c.executemany("INSERT INTO Entries VALUES " + valuesQuery, insertList)

                    # Clear insertList
                    insertList[:] = []
            except sqlite3.Error as er:
                print("%s - Sqlite error: %s" % (self.proc_name, er.message))
                self.logger.debug("%s - Sqlite error: %s" % (self.proc_name, er.message))
            self.conn.commit()


class Task(object):
    def __init__(self, pathToLoad, data):
        self.pathToLoad = pathToLoad
        # Task format is (fileFullPath, hostName, HostID)
        self.data = data

    def __call__(self):
        rowsData = []
        for item in self.data:
            file_fullpath = item[0]
            assert (file_fullpath)
            instanceID = item[1]
            assert (instanceID)
            hostName = item[2]
            assert (hostName)
            hostID = item[3]
            ingest_class_instance = item[4]
            assert(ingest_class_instance)
            try:
                logger.debug("Processing file %s" % file_fullpath)
                ingest_class_instance.processFile(file_fullpath, hostID, instanceID, rowsData)
            except Exception as e:
                logger.error("Error processing: %s (%s)" % (file_fullpath, str(e)))

        return rowsData


def CalculateInstanceID(file_fullpath, ingest_plugin):
    instanceID = ingest_plugin.calculateID(file_fullpath)
    assert(instanceID is not None)

    return instanceID


def GetIDForHosts(fileFullPathList, DB):
    # Returns: (filePath, instanceID, hostname, hostID, ingest_type)
    hostsTest = {}
    hostsProcess = []
    progress_total = 0
    progress_current = 0

    # Determine plugin_type and hostname
    for file_name_fullpath in fileFullPathList:
        hostName = None
        ingest_type = None
        loop_counter = 0
        while True:
            if loop_counter > len(ingest_plugins_types_stack):
                # We ignore empty file from hosts with no appcompat data
                # todo: Omit suppression on verbose mode
                tmp_file_size = file_size(file_name_fullpath)
                if tmp_file_size > 500:
                    logger.warning("No ingest plugin could process: %s (skipping file) [size: %d]" %
                                   (ntpath.basename(file_name_fullpath), tmp_file_size))
                break
            ingest_type = ingest_plugins_types_stack[0]
            if ingest_plugins[ingest_type].matchFileNameFilter(file_name_fullpath):
                # Check magic:
                try:
                    if ingest_plugins[ingest_type].checkMagic(file_name_fullpath):
                        # Magic OK, go with this plugin
                        hostName = ingest_plugins[ingest_type].getHostName(file_name_fullpath)
                        break
                except Exception as e:
                    logger.exception("Error processing: %s (%s)" % (file_name_fullpath, str(e)))
            # Emulate stack with list to minimize internal looping (place last used plugin at the top)
            ingest_plugins_types_stack.remove(ingest_type)
            ingest_plugins_types_stack.insert(len(ingest_plugins_types_stack), ingest_type)
            loop_counter += 1
        if hostName is not None:
            if hostName in hostsTest:
                hostsTest[hostName].append((file_name_fullpath, ingest_plugins[ingest_type]))
            else:
                hostsTest[hostName] = []
                hostsTest[hostName].append((file_name_fullpath, ingest_plugins[ingest_type]))

    progress_total = len(hostsTest.keys())
    # Iterate over hosts. If host exists in DB grab rowID else create and grab rowID.
    conn = DB.appGetConn()
    with closing(conn.cursor()) as c:
        for hostName in hostsTest.keys():
            assert(hostName)
            logger.debug("Processing host: %s" % hostName)
            # Check if Host exists
            c.execute("SELECT count(*) FROM Hosts WHERE HostName = '%s'" % hostName)
            data = c.fetchone()[0]
            if (data != 0):
                # Host already has at least one instance in the DB
                c.execute("SELECT HostID, Instances FROM Hosts WHERE HostName = '%s'" % hostName)
                data = c.fetchone()
                tmpHostID = data[0]
                tmpInstances = eval(data[1])
                for (file_fullpath, ingest_plugin) in hostsTest[hostName]:
                    logger.debug("Grabbing instanceID from file: %s" % file_fullpath)
                    try:
                        instance_ID = CalculateInstanceID(file_fullpath, ingest_plugin)
                    except Exception:
                        logger.error("Error parsing: %s (skipping)" % file_fullpath)
                        traceback.print_exc(file=sys.stdout)
                    else:
                        if str(instance_ID) not in tmpInstances:
                            tmpInstances.append(str(instance_ID))
                            hostsProcess.append((file_fullpath, instance_ID, hostName, tmpHostID, ingest_plugin))
                        else:
                            logger.debug("Duplicate host and instance found: %s" %hostName)
                            continue
                # Save updated Instances list
                c.execute("UPDATE Hosts SET Instances = %s, InstancesCounter = %d WHERE HostName = '%s'" % ('"' + str(repr(tmpInstances)) + '"', len(tmpInstances), hostName))
            else:
                # Host does not exist. Add instance and grab the host ID.
                tmpInstances = []
                newInstances = []
                for (file_fullpath, ingest_plugin) in hostsTest[hostName]:
                    try:
                        instance_ID = CalculateInstanceID(file_fullpath, ingest_plugin)
                    except Exception:
                        logger.error("Error parsing: %s (skipping)" % file_fullpath)
                        traceback.print_exc(file=sys.stdout)
                    else:
                        if str(instance_ID) not in tmpInstances:
                            tmpInstances.append(str(instance_ID))
                            newInstances.append((file_fullpath, instance_ID, ingest_plugin))

                c.execute("INSERT INTO Hosts VALUES (NULL,%s,%s,%d,%d,%d)" % ('"' + hostName + '"', '"' + str(repr(tmpInstances)) + '"', len(tmpInstances), 0, 0))
                tmpHostID = c.lastrowid
                for (file_fullpath, instance_ID, ingest_plugin) in newInstances:
                    # todo: Do we want/need each row to track from what instance it came?
                    hostsProcess.append((file_fullpath, instance_ID, hostName, tmpHostID, ingest_plugin))
            # Update progress
            progress_current += 1
            if settings.logger_getDebugMode():
                status_extra_data = " [RAM: %d%%]" % psutil_phymem_usage()
            else: status_extra_data = ""
            # logger.debug("Pre-process new hosts/instances%s" % status_extra_data)
            logger.info(update_progress(min(1, float(progress_current) / float(progress_total)), "Calculate ID's for new hosts/instances%s" % status_extra_data, True))
        conn.commit()

    # Return hosts to be processed
    return hostsProcess


def processArchives(filename, file_filter):
    # Process zip file if required and return a list of files to process
    files_to_process = []

    if filename.endswith('.zip'):
        try:
            zip_archive_filename = filename
            # Open the zip archive:
            zip_archive = zipfile.ZipFile(zip_archive_filename, "r")
            zipFileList = zip_archive.namelist()
            zip_archive.close()
            countTotalFiles = len(zipFileList)
            logger.info("Total files in %s: %d" % (zip_archive_filename, countTotalFiles))
            logger.info("Hold on while we check the zipped files...")

            for zipped_filename in zipFileList:
                if re.match(file_filter, zipped_filename):
                    files_to_process.append(os.path.join(zip_archive_filename, zipped_filename))
            if len(files_to_process) == 0:
                logger.error("No valid files found!")
        except (IOError, zipfile.BadZipfile, struct.error), err:
            logger.error("Error reading zip archive: %s" % zip_archive_filename)
            exit(-1)
    else:
        files_to_process.append(filename)
    return files_to_process

def searchFolders(pathToLoad, file_filter):
    # Walk folder recursively and build and return a list of files
    files_to_process = []

    # Process
    for root, directories, filenames in os.walk(pathToLoad):
        for dir in directories:
            files_to_process.extend(searchFolders(os.path.join(pathToLoad, dir), file_filter))
        for filename in filenames:
            if re.match(file_filter, os.path.join(pathToLoad, filename)):
                files_to_process.extend(processArchives(os.path.join(pathToLoad, filename), file_filter))
            else:
                logger.warning("Skiping file, no ingest plugin found to process: %s" % filename)
        break
    return files_to_process


def searchRedLineAudits(pathToLoad):
    # Walk folder recursively and build the list of Redline registry audits to process
    files_to_process = []

    # Process
    for root, directories, filenames in os.walk(pathToLoad):
        for dir in directories:
            files_to_process.extend(searchRedLineAudits(os.path.join(pathToLoad, dir)))
        for filename in filenames:
            if re.match('w32registryapi\..{22}$', filename):
                files_to_process.append(os.path.join(pathToLoad, filename))
        break
    return files_to_process


def appLoadMP(pathToLoad, dbfilenameFullPath, maxCores, governorOffFlag):
    global _tasksPerJob

    files_to_process = []
    conn = None

    # Start timer
    t0 = datetime.now()

    logger.debug("Starting appLoadMP")
    # Calculate aggreagate file_filter for all ingest types supported:
    file_filter = '|'.join([v.getFileNameFilter() for k,v in ingest_plugins.iteritems()])
    # Add zip extension
    file_filter += "|.*\.zip"

    # Check if we're loading Redline data
    if os.path.isdir(pathToLoad) and os.path.basename(pathToLoad).lower() == 'RedlineAudits'.lower():
        files_to_process = searchRedLineAudits(pathToLoad)
    else:
        # Search for all files to be processed
        if os.path.isdir(pathToLoad):
            files_to_process = searchFolders(pathToLoad, file_filter)
        else:
            files_to_process = processArchives(pathToLoad, file_filter)

    if files_to_process:
        # Init DB if required
        DB = appDB.DBClass(dbfilenameFullPath, True, settings.__version__)
        conn = DB.appConnectDB()

        # Extract hostnames, grab existing host IDs from DB and calculate instance ID for new IDs to be ingested:
        instancesToProcess = []
        instancesToProcess += GetIDForHosts(files_to_process, DB)
        countInstancesToProcess = len(instancesToProcess)
        logger.info("Found %d new instances" % (countInstancesToProcess))

        # Setup producers/consumers initial counts
        num_consumers = 1
        num_producers = 1

        # Setup MPEngine
        mpe = MPEngineProdCons(maxCores, appLoadProd, appLoadCons, governorOffFlag)

        # Reduce _tasksPerJob for small jobs
        if countInstancesToProcess < _tasksPerJob: _tasksPerJob = 1

        # Create task list
        task_list = []
        instancesPerJob = _tasksPerJob
        num_tasks = 0
        for chunk in chunks(instancesToProcess, instancesPerJob):
            # todo: We no longer need pathToLoad as tasks include the fullpath now
            task_list.append(Task(pathToLoad, chunk))
            num_tasks += 1

        if num_tasks > 0:
            # Check if we have to drop indexes to speedup insertions
            # todo: Research ratio of existing hosts to new hosts were this makes sense
            if countInstancesToProcess > 1000 or DB.CountHosts() < 1000:
                DB.appDropIndexesDB()

            # Queue tasks for Producers
            mpe.addTaskList(task_list)

            # Start procs
            mpe.startProducers(num_producers)
            mpe.startConsumers(num_consumers, [dbfilenameFullPath])
            # mpe.addProducer()

            # Control loop
            while mpe.working():
                time.sleep(1.0)
                (num_producers,num_consumers,num_tasks,progress_producers,progress_consumers) = mpe.getProgress()
                elapsed_time = datetime.now() - t0
                mean_loadtime_per_host = (elapsed_time) / max(1, _tasksPerJob * progress_consumers)
                pending_hosts = ((num_tasks * _tasksPerJob) - (_tasksPerJob * progress_consumers))
                etr = (mean_loadtime_per_host * pending_hosts)
                eta = t0 + elapsed_time + etr
                ett = (eta - t0)
                if settings.logger_getDebugMode(): status_extra_data = " Prod: %s Cons: %s (%d -> %d -> %d: %d) [RAM: %d%% / Obj: %d / ETH: %s / ETA: %s / ETT: %s]" % \
                                                                       (num_producers, num_consumers, num_tasks, progress_producers, progress_consumers, progress_producers - progress_consumers,
                     psutil_phymem_usage(), len(gc.get_objects()),
                     mean_loadtime_per_host if progress_consumers * _tasksPerJob > 100 else "N/A",
                     str(eta.time()).split(".")[0] if progress_consumers * _tasksPerJob > 100 else "N/A",
                     str(ett).split(".")[0] if progress_consumers * _tasksPerJob > 100 else "N/A")
                else: status_extra_data = ""
                # logger.info("Parsing files%s" % status_extra_data)

                logger.info(update_progress(min(1,float(progress_consumers) / float(num_tasks)), "Parsing files%s" % status_extra_data, True))
                mpe.rebalance()

            del mpe

        # Stop timer
        elapsed_time = datetime.now() - t0
        mean_loadtime_per_host = (elapsed_time) / max(1, countInstancesToProcess)
        logger.info("Load speed: %s seconds / file" % (mean_loadtime_per_host))
        logger.info("Load time: %s" % (str(elapsed_time).split(".")[0]))
    else:
        logger.info("Found no files to process!")