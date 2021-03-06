#!/usr/local/bin/python3
'''
S3GetBucketStats
version 1.0.0
By Andre Couture
Coveo Challenge
'''
import concurrent
import csv
import gzip
import itertools
import json
import math
import os
import re
import sys
import time
from argparse import ArgumentParser
from datetime import timedelta
from io import BytesIO, StringIO
from threading import Thread

import boto3
import pandas as pd
import requests
from botocore.config import Config

groups_dict = {'REDUCED_REDUNDANCY', 'STANDARD', 'STANDARD_IA'}
sizes_name = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]
csv_columns = ['Bucket', 'Key', 'ETag', 'Size', 'LastModified', 'StorageClass']

def put_inventory_configuration(bucket):
    s3.put_bucket_inventory_configuration(
        Bucket=bucket,
        Id=bucket + "-inventory",
        InventoryConfiguration={
            'Destination': {
                # Put the inventory files into the same bucket.
                'S3BucketDestination': {
                    # A bucket ARN rather than a bucket name.
                    'Bucket': 'arn:aws:s3:::' + bucket,
                    'Format': 'CSV',
                    #// Not needed if everything is in the same account.
                    #// AccountId: '111222333444',
                    #// Do NOT add a trailing '/' to this prefix; it will be added 
                    #// by S3 Inventory when creating the keys regardless, and so
                    #// the key will start with 'inventory//' if you add one here.
                    'Prefix': 'inventory'
                }
            },
            'Id': bucket + "-inventory",
            'IncludedObjectVersions': 'All',
            'IsEnabled': True,
            'Schedule': {
            'Frequency': 'Daily'
            },
            # Add all the mentioned optional fields.
            'OptionalFields': [
                'Size',
                'LastModifiedDate',
                'StorageClass',
                'ETag',
                'IsMultipartUploaded',
                'ReplicationStatus',
                'EncryptionStatus'
            ]
        }
    )


global grand_total_objects
global grand_total_size
global grand_total_cost


class Settings(object):
    def __init__(self):
        self._REGEX = ".*"
        self._BUCKET_LIST_REGEX = None
        self._KEY_PREFIX = '/'
        self._DISPLAY_SIZE = 0
        self._REGION_FILTER = '.*'
        self._OUTPUT_FILE = ''
        self._VERBOSE = 1
        self._CACHE = None
        self._REFRESHCACHE = None
        self._INVENTORY = None
        self._S3SELECT = None
        self._LOWMEMORY = False
        self._THREADED = 0
        self._MAX_THREADS = 0
        self._BUCKETS = None
        self._PUT_INVENTORY = False

    def set_put_inventory(self, value):
        self._PUT_INVENTORY = value

    def set_buckets(self, value):
        self._BUCKETS = value

    def set_maxthreads(self, value):
        self._MAX_THREADS = value

    def set_threaded(self, value):
        self._THREADED = value

    def set_lowmemory(self, value):
        self._LOWMEMORY = value

    def set_s3select(self, value):
        self._S3SELECT = value

    def set_inventory(self, value):
        self._INVENTORY = value

    def set_refresh_cache(self, value):
        self._REFRESHCACHE = value

    def set_verbose(self, value):
        self._VERBOSE = value

    def set_cache(self, value):
        self._CACHE = value

    def set_output_file(self, output_file):
        self._OUTPUT_FILE = output_file

    def set_region_filter(self, regex):
        self._REGION_FILTER = regex

    def set_display_size(self, value):
        self._DISPLAY_SIZE = value

    def set_regex(self, regex):
        self._REGEX = regex

    def set_bucket_regex(self, regex):
        self._BUCKET_LIST_REGEX = regex

    def set_key_prefix(self, regex):
        self._KEY_PREFIX = regex


def append_output(results):
    with open(settings._OUTPUT_FILE, "a") as output:
        output.write(results)


def get_region(bucket_name):
    try:
        response = requests.get("http://" + bucket_name + ".s3.amazonaws.com/")
        region = response.headers.get("x-amz-bucket-region")
        return region
    except Exception as e:
        print("Error: couldn't connect to '{0}' bucket. Details: {1}".format(response, e))


def get_encryption(bucket_name):
    try:
        encryption = {
            "ServerSizeEncryption": s3.get_bucket_encryption(Bucket=bucket_name)['ServerSideEncryptionConfiguration'][
                'Rules']}
    except Exception:
        encryption = "Disabled"
    return encryption


def get_website(bucket_name):
    try:
        website = s3.get_bucket_website(Bucket=bucket_name)
        response = {
            "IndexDocument": website.get("IndexDocument", None),
            "ErrorDocument": website.get("ErrorDocument", None)
        }
    except Exception:
        response = {}
    return response


def get_location(bucket_name):
    try:
        location = s3.get_bucket_location(Bucket=bucket_name)['LocationConstraint']
    except Exception:
        location = None
    return location


def get_versioning(bucket_name):
    try:
        versioning = s3.get_bucket_versioning(Bucket=bucket_name)['Status']
    except Exception:
        versioning = "Disabled"
    return versioning


def get_grantees(bucket_name):
    acl = boto3.resource("s3").Bucket(bucket_name).Acl()
    grantees = []
    try:
        groups = itertools.groupby(sorted(acl.grants, key=lambda k: k['Permission']), lambda k: k['Permission'])
    except Exception:
        return grantees
    for k, g in groups:
        perm = {}
        gt = []
        for x in g:
            try:
                gt.extend([x['Grantee']['DisplayName']])
            except Exception as e:
                try:
                    if "URI" in x['Grantee']:
                        gt.extend([x['Grantee']['URI']])
                except Exception as e:
                    print(e)
                continue
        grantees.append({'Permission': k, 'Grantees': gt})
    return grantees


def get_acceleration(bucket_name):
    try:
        s3_client = boto3.client("s3", config=Config(s3={'use_accelerate_endpoint': True}))
        status = s3_client.get_bucket_accelerate_configuration(Bucket=bucket_name)['Status']
    except Exception:
        status = "Disabled"
    return status


def get_object_lock_configuration(bucket_name):
    try:
        bucket_configuration = s3.get_object_lock_configuration(Bucket=bucket_name)
        response = bucket_configuration['ObjectLockConfiguration']['ObjectLockEnabled']
    except Exception:
        response = "Disabled"
    return response


def get_inventory_configurations(bucket_name):
    response = []
    try:
        bucket_configuration = s3.list_bucket_inventory_configurations(Bucket=bucket_name)
        if "InventoryConfigurationList" in bucket_configuration:
            for inventory_bucket in bucket_configuration['InventoryConfigurationList']:
                response.append(
                    {
                        'Id': inventory_bucket['Id'],
                        'IsEnabled': inventory_bucket['IsEnabled'],
                        'Bucket': inventory_bucket['Destination']['S3BucketDestination']['Bucket'].split(':')[-1],
                        'Format': inventory_bucket['Destination']['S3BucketDestination']['Format'],
                        'Versions': inventory_bucket['IncludedObjectVersions']
                    }
                )
    except Exception:
        return response
    return response


def get_replication(bucket_name):
    response = []
    try:
        configurations = s3.get_bucket_replication(Bucket=bucket_name)
        if "ReplicationConfiguration" in configurations and "Rules" in configurations['ReplicationConfiguration']:
            for configuration in configurations['ReplicationConfiguration']['Rules']:
                response.append(
                    {
                        'Id': configuration['ID'],
                        'Status': configuration['Status'],
                        'Destination': configuration['Destination']
                    }
                )
    except Exception:
        response = []
    return response


def get_policy(bucket_name):
    try:
        policy = s3.get_bucket_policy(Bucket=bucket_name)['Policy']
        response = "Enabled"
    except Exception:
        response = "Disabled"
    return response


def get_analytics(bucket_name):
    try:
        bucket_analytics = s3.list_bucket_analytics_configurations(Bucket=bucket_name)['AnalyticsConfigurationList']
        response = 'Enabled'
    except Exception:
        response = 'Disabled'
    return response


def find_latest_inventory_manifest_key(bucket_name, inventory_bucket, inventory_id):
    kwargs = {'Bucket': inventory_bucket, 'Prefix': bucket_name + "/" + inventory_id + "/"}
    latest = sorted(s3.list_objects_v2(**kwargs)['Contents'], key=lambda obj: obj['LastModified'], reverse=True)
    manifest = next(key['Key'] for key in latest if key['Key'].endswith("manifest.json"))
    return manifest


def load_manifest(bucket_name, key):
    kwargs = {'Bucket': bucket_name, 'Key': key}
    data = s3.get_object(**kwargs)
    contents = json.loads(data['Body'].read())
    return contents


def load_inventory_csv(bucket_name, inventory_ids):
    inv_agg = []
    for inventory in inventory_ids:
        if inventory['Format'] == "CSV" and inventory['IsEnabled']:
            try:
                if settings._VERBOSE > 0:
                    print("Using Inventory Id '{}' for bucket '{}'".format(inventory['Id'], bucket_name), end="\r")
                try:
                    inventory_manifest = find_latest_inventory_manifest_key(bucket_name, inventory['Bucket'],
                                                                        inventory['Id'])
                except KeyError:
                    continue

                if inventory_manifest.__len__() == 0:
                    continue
                manifest = load_manifest(inventory['Bucket'], inventory_manifest)
                if settings._VERBOSE > 2:
                    print("manifest: {}".format(manifest))
                schema = [item.strip() for item in manifest['fileSchema'].split(",")]
                if settings._VERBOSE > 2:
                    print("schema: {}".format(schema))
                    print("files: {}".format(manifest['files'][0]['key']))

                if settings._S3SELECT:
                    inv_agg = pd.concat(
                        s3select_inventory_csv(inventory['Bucket'], files['key'], schema).groupby('StorageClass',
                                                                                                  as_index=False).agg(
                            {'Count': 'sum', 'Size': 'sum', 'LastModifiedDate': 'max'}) for files in
                        manifest['files']).groupby('StorageClass', as_index=False).agg(
                        {'Count': 'sum', 'Size': 'max', 'LastModifiedDate': 'max'})
                else:
                    inv_agg = pd.concat(
                        read_inventory_file(inventory['Bucket'], files['key'], schema).groupby('StorageClass',
                                                                                               as_index=False).agg(
                            {'Count': 'sum', 'Size': 'sum', 'LastModifiedDate': 'max'}) for files in
                        manifest['files']).groupby('StorageClass', as_index=False).agg(
                        {'Count': 'sum', 'Size': 'max', 'LastModifiedDate': 'max'})
                break
            except Exception as e:
                print("load_inventory exception:", e)
                continue
    #    inv_agg.rename(columns={'LastModifiedDate': 'LastModified'})
    return inv_agg


def display_size(size_bytes, sizeformat=-1):
    if sizeformat == -1:
        sizeformat = settings._DISPLAY_SIZE
    i = sizeformat
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return "{0}{1}".format(s, tuple(sizes_name)[i])


def write_cache_csv(bucket_name, objects):
    with open(bucket_name + ".cache", 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
        writer.writeheader()
        for data in objects:
            writer.writerow(data)


def read_cache_csv(bucket_name):
    data = []
    df = pd.read_csv(bucket_name + ".cache")
    for index, row in df.iterrows():
        d = row.to_dict()
        data.append(d)
    return data


'''
Analyse from compressed CSV file in the bucket via S3 Select
'''


def s3select_inventory_csv(bucket_name, key, cols_names):
    content_options = {"FieldDelimiter": ",", 'AllowQuotedRecordDelimiter': False}
    # expression = "select * from s3object"
    size_pos = cols_names.index('Size')
    lastmodified_pos = cols_names.index('LastModifiedDate')
    storage_pos = cols_names.index('StorageClass')
    encryption_pos = cols_names.index('EncryptionStatus')
    expression = "select _{},_{},_{},_{} from s3object".format(size_pos + 1, lastmodified_pos + 1, storage_pos + 1,
                                                               encryption_pos + 1)
    req = s3.select_object_content(
        Bucket=bucket_name,
        Key=key,
        ExpressionType='SQL',
        Expression=expression,
        InputSerialization={'CompressionType': "GZIP", "CSV": content_options},
        OutputSerialization={'CSV': {}},
    )
    records = []
    for event in req['Payload']:
        if "Records" in event:
            records.append(event['Records']['Payload'].decode('utf-8'))
        elif "Stats" in event:
            stats = event['Stats']['Details']
    file_str = "".join(r for r in records)
    if settings._VERBOSE > 2:
        print("s3select strlen: {}  records:{}".format(file_str.__len__(), records.__len__()))
    df = pd.read_csv(StringIO(file_str), names=['Size', 'LastModifiedDate', 'StorageClass', 'EncryptionStatus'])

    aggr = df.groupby('StorageClass').agg({'StorageClass': 'count', 'Size': 'sum', 'LastModifiedDate': 'max'}).rename(
        columns={'StorageClass': 'Count'}).reset_index()
    if settings._VERBOSE > 4:
        print(">>>>", aggr)
    return aggr


'''
Analyse from compressed CSV file in the bucket
'''


def read_inventory_file(bucket_name, key, cols_names):
    if settings._VERBOSE > 1:
        print("read_inventory_file: {} {} {}".format(bucket_name, key, cols_names))
    s3_client = boto3.client("s3", config=Config(s3={'use_accelerate_endpoint': True}))
    try:
        status = s3_client.get_bucket_accelerate_configuration(Bucket=bucket_name)['Status']
        if settings._VERBOSE > 1:
            print("Loading inventory '{:50}' using acceleration {}".format(key, status))
    except Exception:
        # Acceleration not possible
        s3_client = s3
    data = []
    if settings._VERBOSE > 2:
        print("read_inventory file: s3://{}/{}  Schema:{}".format(bucket_name, key, cols_names))
    read_file = s3_client.get_object(Bucket=bucket_name, Key=key)
    gzipfile = gzip.GzipFile(fileobj=BytesIO(read_file['Body'].read()))

    df = pd.read_csv(gzipfile, sep=',', header=None, names=cols_names)

    aggr = df.groupby('StorageClass').agg({'StorageClass': 'count', 'Size': 'sum', 'LastModifiedDate': 'max'}).rename(
        columns={'StorageClass': 'Count'}).reset_index()
    if settings._VERBOSE > 2:
        print("read_inventory read {} objects from {}.".format(data.__len__(), key))
    return aggr


def add_bool_arg(parser, name, default=False, description=""):
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("-" + name, dest=name, action="store_true",
                       help=description + (" (DEFAULT) " if default else ""))
    group.add_argument("-no-" + name, dest=name, action="store_false",
                       help="Do not " + description + (" (DEFAULT) " if not default else ""))
    parser.set_defaults(**{name: default})


def get_bucket_cost_for_storageclass(bucket_region, storageClass, storageSize):
    objects_size = storageSize / math.pow(1024, 3)
    pricing = get_priceDimensions_for_region_volume(bucket_region, storageClass)
    cost = 0
    while objects_size > 0:
        for x in pricing:
            if x["endRange"] == "Inf":
                endRange = sys.maxsize
            else:
                endRange = int(x["endRange"])
            if objects_size < endRange:
                pricePerUnit = x['pricePerUnit']['USD']
                cost += (float(objects_size) * float(pricePerUnit))
                return cost
            else:
                objects_size -= endRange
    return -1


def threaded_analyse_bucket_contents(bucket_name, result=None, i=0):
    processing_start = time.perf_counter()

    prefix = settings._KEY_PREFIX
    delimiter = "/"
    start_after = ""
    aggs = []
    if settings._CACHE and os.path.isfile(bucket_name + ".cache"):
        print("Processing via local Cache for bucket {}".format(bucket_name), end="\r")
        aggs = read_cache_csv(bucket_name)
    elif settings._INVENTORY:
        inventory = get_inventory_configurations(bucket_name)
        if inventory != "Disabled" and inventory.__len__() > 0:
            print("Processing via Inventory for bucket {}".format(bucket_name), end="\r")
            aggs = load_inventory_csv(bucket_name, inventory)

    if aggs.__len__() == 0:
        # at this point we could not find any data from the cache or inventory and we have to revert to listing all objects from the bucket
        print("Processing via ListObjects for bucket {}".format(bucket_name), end="\r")
        prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
        start_after = (start_after or prefix) if prefix.endswith(delimiter) else start_after
        s3_paginator = s3.get_paginator("list_objects_v2")
        if settings._CACHE and settings._REFRESHCACHE:
            for p in s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                           PaginationConfig={'PageSize': 1000}):
                write_cache_csv(bucket_name, p.get('Contents'))
        try:
            if settings._LOWMEMORY:
                # low memory
                datas = pd.concat((pd.DataFrame(d.get("Contents"),
                                                columns=['StorageClass', 'Size', 'LastModified']).groupby(
                    ['StorageClass']).agg({'StorageClass': 'count', 'Size': 'sum', 'LastModified': 'max'}).rename(
                    columns={'StorageClass': 'Count'}) for d in
                    s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                          PaginationConfig={'PageSize': 1000}))).groupby(
                    'StorageClass').agg({'Count': 'sum', 'Size': 'sum', 'LastModified': 'max'})
                aggs = (
                    datas.groupby(['StorageClass']).agg({'Count': 'sum', 'Size': 'sum', 'LastModified': 'max'}).rename(
                        columns={'StorageClass': 'Count'}).reset_index())
            else:
                # high memory
                datas = pd.concat(
                    pd.DataFrame(d.get("Contents"), columns=['StorageClass', 'Size', 'LastModified']) for d in
                    s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                          PaginationConfig={'PageSize': 1000}))
                aggs = (datas.groupby(['StorageClass']).agg(
                    {'StorageClass': 'count', 'Size': 'sum', 'LastModified': 'max'}).rename(
                    columns={'StorageClass': 'Count'}).reset_index())

        except Exception as e:
            if settings._VERBOSE > 1: print(e)
            return []

    bucket_objects = aggs['Count'].sum()
    bucket_size = aggs['Size'].sum()
    if 'LastModified' in aggs:
        bucket_last = aggs['LastModified'].max()
    else:
        bucket_last = aggs['LastModifiedDate'].max()

    bucket_cost = 0.0
    bucket = boto3.resource("s3").Bucket(bucket_name)
    bucket_region = get_region(bucket_name)
    content = aggs.to_dict('rows')
    for storageClass in content:
        cost = get_bucket_cost_for_storageclass(bucket_region, storageClass['StorageClass'], storageClass['Size'])
        if cost > 0:
            storageClass['Cost'] = "${:,.2f}".format(cost)
            bucket_cost += cost

    if bucket_cost > 0:
        bucket_cost_str = "${:,.2f}".format(bucket_cost)
    else:
        bucket_cost_str = "n/a"
    bucket_stats = [
        {
            'Name': bucket_name,
            'CreationDate': str(bucket.creation_date),
            'LastModified': str(bucket_last),
            'Versioning': get_versioning(bucket_name),
            'WebSite': get_website(bucket_name),
            'Analytics': get_analytics(bucket_name),
            'Acceleration': get_acceleration(bucket_name),
            'Replication': get_replication(bucket_name),
            'Policy': get_policy(bucket_name),
            'ObjectLock': get_object_lock_configuration(bucket_name),
            'Inventory': get_inventory_configurations(bucket_name),
            'Region': bucket_region,
            'LocationConstraint': get_location(bucket_name),
            'Grantee': get_grantees(bucket_name),
            'Encryption': get_encryption(bucket_name),
            'Size': display_size(bucket_size),
            'Count': bucket_objects,
            'Cost': bucket_cost_str,
            'Content': content
        }
    ]

    global grand_total_cost
    global grand_total_size
    global grand_total_objects
    grand_total_cost += round(bucket_cost, 2)
    grand_total_objects += bucket_objects
    grand_total_size += bucket_size

    bucket_processing_time = timedelta(milliseconds=round(1000 * (time.perf_counter() - processing_start)))

    if result is not None:
        result[i] = bucket_stats, bucket_processing_time
    else:
        return bucket_stats, bucket_processing_time, "{:60}{:>30}{:>20}{:>20}{:>30}{:>20}{:>40}".format(
            bucket_stats[0].get('Name'),
            bucket_stats[0]['CreationDate'],
            bucket_stats[0]['Count'],
            bucket_stats[0]['Size'], bucket_stats[0]['LastModified'],
            bucket_stats[0]['Cost'],
            str(bucket_processing_time))

    print(
        "{:60}{:>30}{:>20}{:>20}{:>30}{:>20}{:>40}".format(bucket_stats[0].get('Name'),
                                                           bucket_stats[0]['CreationDate'],
                                                           bucket_stats[0]['Count'],
                                                           bucket_stats[0]['Size'], bucket_stats[0]['LastModified'],
                                                           bucket_stats[0]['Cost'],
                                                           str(bucket_processing_time)),
        file=sys.stderr)


def analyse_bucket_contents(bucket_name):
    processing_start = time.perf_counter()

    prefix = settings._KEY_PREFIX
    delimiter = "/"
    start_after = ""
    aggs = []
    if settings._CACHE and os.path.isfile(bucket_name + ".cache"):
        print("Processing via local Cache for bucket {}".format(bucket_name), end="\r")
        aggs = read_cache_csv(bucket_name)
    elif settings._INVENTORY:
        inventory = get_inventory_configurations(bucket_name)
        if inventory != "Disabled" and inventory.__len__() > 0:
            print("Processing via Inventory for bucket {}".format(bucket_name), end="\r")
            aggs = load_inventory_csv(bucket_name, inventory)
        elif settings._PUT_INVENTORY:
           put_inventory_configuration(bucket_name)

    if aggs.__len__() == 0:
        # at this point we could not find any data from the cache or inventory and we have to revert to listing all objects from the bucket
        print("Processing via ListObjects for bucket {}".format(bucket_name), end="\r")
        prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
        start_after = (start_after or prefix) if prefix.endswith(delimiter) else start_after
        s3_paginator = s3.get_paginator("list_objects_v2")
        if settings._CACHE and settings._REFRESHCACHE:
            for p in s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                           PaginationConfig={'PageSize': 1000}):
                write_cache_csv(bucket_name, p.get('Contents'))
        try:
            if settings._LOWMEMORY:
                # low memory
                datas = pd.concat((pd.DataFrame(d.get("Contents"),
                                                columns=['StorageClass', 'Size', 'LastModified']).groupby(
                    ['StorageClass']).agg({'StorageClass': 'count', 'Size': 'sum', 'LastModified': 'max'}).rename(
                    columns={'StorageClass': 'Count'}) for d in
                    s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                          PaginationConfig={'PageSize': 1000}))).groupby(
                    'StorageClass').agg({'Count': 'sum', 'Size': 'sum', 'LastModified': 'max'})
                aggs = (
                    datas.groupby(['StorageClass']).agg({'Count': 'sum', 'Size': 'sum', 'LastModified': 'max'}).rename(
                        columns={'StorageClass': 'Count'}).reset_index())
            else:
                # high memory
                datas = pd.concat(
                    pd.DataFrame(d.get("Contents"), columns=['StorageClass', 'Size', 'LastModified']) for d in
                    s3_paginator.paginate(Bucket=bucket_name, Prefix=prefix, StartAfter=start_after,
                                          PaginationConfig={'PageSize': 1000}))
                aggs = (datas.groupby(['StorageClass']).agg(
                    {'StorageClass': 'count', 'Size': 'sum', 'LastModified': 'max'}).rename(
                    columns={'StorageClass': 'Count'}).reset_index())

        except Exception as e:
            if settings._VERBOSE > 1: print(e)
            return []

    bucket_objects = aggs['Count'].sum()
    bucket_size = aggs['Size'].sum()
    if 'LastModified' in aggs:
        bucket_last = aggs['LastModified'].max()
    else:
        bucket_last = aggs['LastModifiedDate'].max()

    bucket_cost = 0.0
    bucket = boto3.resource("s3").Bucket(bucket_name)
    bucket_region = get_region(bucket_name)
    content = aggs.to_dict('rows')
    for storageClass in content:
        cost = get_bucket_cost_for_storageclass(bucket_region, storageClass['StorageClass'], storageClass['Size'])
        if cost > 0:
            storageClass['Cost'] = "${:,.2f}".format(cost)
            bucket_cost += cost

    if bucket_cost > 0:
        bucket_cost_str = "${:,.2f}".format(bucket_cost)
    else:
        bucket_cost_str = "n/a"
    bucket_stats = [
        {
            'Name': bucket_name,
            'CreationDate': str(bucket.creation_date),
            'LastModified': str(bucket_last),
            'Versioning': get_versioning(bucket_name),
            'WebSite': get_website(bucket_name),
            'Analytics': get_analytics(bucket_name),
            'Acceleration': get_acceleration(bucket_name),
            'Replication': get_replication(bucket_name),
            'Policy': get_policy(bucket_name),
            'ObjectLock': get_object_lock_configuration(bucket_name),
            'Inventory': get_inventory_configurations(bucket_name),
            'Region': bucket_region,
            'LocationConstraint': get_location(bucket_name),
            'Grantee': get_grantees(bucket_name),
            'Encryption': get_encryption(bucket_name),
            'Size': display_size(bucket_size),
            'Count': bucket_objects,
            'Cost': bucket_cost_str,
            'Content': content
        }
    ]

    global grand_total_cost
    global grand_total_size
    global grand_total_objects
    grand_total_cost += round(bucket_cost, 2)
    grand_total_objects += bucket_objects
    grand_total_size += bucket_size

    bucket_processing_time = timedelta(milliseconds=round(1000 * (time.perf_counter() - processing_start)))

    yield bucket_stats, bucket_processing_time


def load_aws_pricing(region, vol):
    pricing = boto3.client('pricing', "us-east-1")
    prefix = "/"
    delimiter = "/"
    start_after = ""
    prefix = prefix[1:] if prefix.startswith(delimiter) else prefix
    start_after = (start_after or prefix) if prefix.endswith(delimiter) else start_after
    loc = describe_region(region)
    try:
        pricing_page = pricing.get_paginator("get_products")
        for p in pricing_page.paginate(ServiceCode='AmazonS3',
                                       Filters=[
                                           {'Type': 'TERM_MATCH', 'Field': 'location', 'Value': loc},
                                           # {'Type': 'TERM_MATCH', 'Field': 'servicename', 'Value': 'Amazon Simple Storage Service'},
                                           # {'Type': 'TERM_MATCH', 'Field': 'usagetype', 'Value': 'TimedStorage-ByteHrs'}
                                           # {'Type': 'TERM_MATCH', 'Field': 'volumeType', 'Value': 'Standard - Infrequent Access'}
                                           # {'Type': 'TERM_MATCH', 'Field': 'volumeType', 'Value': 'Standard'}
                                           {'Type': 'TERM_MATCH', 'Field': 'volumeType', 'Value': vol}
                                       ],
                                       PaginationConfig={'PageSize': 100}):
            x = p.get('PriceList')
            y = p.get('PriceList')[0]

            return json.loads(p.get('PriceList')[0])
    except Exception as e:
        print("EXCEPTION in get_products(region={},vol={}, loc={})".format(region, vol, loc), e)
    return {'terms': {'OnDemand': []}}


def describe_region(region_id):
    # First try via API, this would allow to pickup on new regions as they arise but does requires more permissions.
    try:
        # ec2 = boto3.client("ec2")
        # ec2_responses = ec2.describe_regions()
        ssm_client = boto3.client('ssm')
        tmp = '/aws/service/global-infrastructure/regions/%s/longName' % region_id
        ssm_response = ssm_client.get_parameter(Name=tmp)
        region_name = ssm_response['Parameter']['Value']
    except Exception:
        regions = {
            'eu-north-1': 'Europe (Stockholm)',
            'ap-south-1': 'Asia Pacific (Mumbai)',
            'eu-west-3': 'Europe (Paris)',
            'eu-west-2': 'Europe (London)',
            'eu-west-1': 'Europe (Ireland)',
            'ap-northeast-2': 'Asia Pacific (Seoul)',
            'ap-northeast-1': 'Asia Pacific (Tokyo)',
            'sa-east-1': 'South America (Sao Paulo)',
            'ca-central-1': 'Canada (Central)',
            'ap-southeast-1': 'Asia Pacific (Singapore)',
            'ap-southeast-2': 'Asia Pacific (Sydney)',
            'eu-central-1': 'Europe (Frankfurt)',
            'us-east-1': 'US East (N. Virginia)',
            'us-east-2': 'US East (Ohio)',
            'us-west-1': 'US West (N. California)',
            'us-west-2': 'US West (Oregon)'
        }
        region_name = regions.get(region_id)
    return region_name


def get_priceDimensions_for_region_volume(region, volumeType):
    volume_types = {
        "STANDARD": "Standard",
        "STANDARD_IA": "Standard - Infrequent Access",
        "ONEZONE_IA": "One Zone - Infrequent Access",
        "REDUCED_REDUNDANCY": "Reduced Redundancy",
        "GLACIER": "Amazon Glacier"
    }
    volumeTypeLong = volume_types.get(volumeType)
    if volumeTypeLong is None:
        print("Could not find entry for volumeType ", volumeType)
        return []
    price_list = load_aws_pricing(region, volumeTypeLong).get('terms')['OnDemand']
    price_dimensions = []
    if len(price_list) == 0:
        return []
    for k, v in price_list.items():
        for a, b in v['priceDimensions'].items():
            price_dimensions.append(b)

    return sorted(price_dimensions, key=lambda i: int(i['beginRange']))


def set_arguments_parameters(parser):
    parser.add_argument("-v", "--verbose", dest="verbose", required=False, default=1,
                        help="Verbose level, 0 for quiet.")
    parser.add_argument("-l", "--list-regex", dest="bucket_regex", required=False, default=None,
                        help="Regex to filter which buckets to process. Use '.*' to scan all.")
    parser.add_argument("-k", "--key-prefix", dest="key_prefix", required=False, default='/',
                        help="Key prefix to filter on, default='/'")
    parser.add_argument("-r", "--region-regex", dest="region_filter", required=False, default='.*',
                        help="Regex Region filter")
    parser.add_argument("-o", "--output", dest="output", required=False, default=None, help="Output to File")
    parser.add_argument("-s", "--display-size", dest="size", type=str, required=False, default="GB",
                        help="Possible values:  [ B | KB | MB | GB | TB | PB | EB | ZB | YB ]")
    parser.add_argument("-b", "--buckets", dest="buckets", type=str, nargs='+', required=False, default="",
                        help="List of specific buckets to scan. Multiple seperated by space")
    parser.add_argument("-t", "--thread-type", dest="threaded", type=int, required=False, default=1,
                        help="Thread type, 0 to disable, 1 for Process (Default), 2 for Pool")
    parser.add_argument("-m", "--max-threads", dest="maxthreads", type=int, required=False, default=1,
                        help="Max number of pool threads")
    parser.add_argument("-i", "--put-inventory", dest="put_inventory", action="store_true", required=False, default=False, help="Add inventory if not exist")

    add_bool_arg(parser, "cache", False, "Use Cache file if available")
    add_bool_arg(parser, "refresh", False, "Force Refresh Cache")
    add_bool_arg(parser, "inventory", True, "Use Inventory if exist")
    add_bool_arg(parser, "s3select", True, "Use S3 Select to parse inventory result files")
    add_bool_arg(parser, "lowmemory", False, "If you have low memory.")
    # add_bool_arg(parser, "threaded", True, "Use Multi-Thread.")

    arguments = parser.parse_args()

    if arguments.verbose: settings.set_verbose(int(arguments.verbose))
    if arguments.buckets: settings.set_buckets(arguments.buckets)
    if arguments.bucket_regex: settings.set_bucket_regex(arguments.bucket_regex)
    if arguments.region_filter: settings.set_region_filter(arguments.region_filter)
    if arguments.key_prefix: settings.set_key_prefix(arguments.key_prefix)
    if arguments.output is not None: settings.set_output_file(arguments.output)

    if arguments.put_inventory: settings.set_put_inventory(arguments.put_inventory)

    settings.set_refresh_cache(arguments.refresh)
    settings.set_cache(arguments.cache)
    settings.set_inventory(arguments.inventory)
    settings.set_s3select(arguments.s3select)
    settings.set_lowmemory(arguments.lowmemory)
    settings.set_threaded(arguments.threaded)
    settings.set_maxthreads(arguments.maxthreads)

    settings.set_display_size(sizes_name.index(arguments.size))


if __name__ == "__main__":
    # set process start timer
    realstart = time.perf_counter()

    # Initialize settings with default values
    settings = Settings()

    parser = ArgumentParser()
    set_arguments_parameters(parser)

    try:
        s3 = boto3.client('s3')
        buckets = s3.list_buckets()
    except Exception as e:
        print("Try setting the environment variables AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/AWS_SESSION_TOKEN")
        exit(1)

    buckets_stats_array = []
    bucket_list = []

    # Filter buckets based on bucket list regex parameter.
    if settings._BUCKET_LIST_REGEX is not None:
        bucket_list = [i['Name'] for i in buckets['Buckets'] if re.match(settings._BUCKET_LIST_REGEX, i['Name'])]

    # If no bucket found we simply assume that the parameter is a targeted bucket name
    if settings._BUCKETS is not None:
        bucket_list.extend(settings._BUCKETS)

    if bucket_list.__len__() == 0:
        print("No buckets to scan found. run with -h to see available options")
        exit(0)

    # Filter buckets based on requested region filter.
    [bucket_list.remove(b) for b in bucket_list if not re.match(settings._REGION_FILTER, get_region(b))]

    grand_total_size = 0
    grand_total_objects = 0
    grand_total_cost = 0

    print("{:60}{:>30}{:>20}{:>20}{:>30}{:>20}{:>40}".format("Bucket", "Created", "Objects", "Size", "LastModified",
                                                             "Cost (USD)", "Processing Time"), file=sys.stderr)
    buckets = []

    if settings._THREADED == 1:
        buckets_results = [{} for i in bucket_list]
        threads = []

        for i in range(len(bucket_list)):
            process = Thread(target=threaded_analyse_bucket_contents, args=[bucket_list[i], buckets_results, i])
            process.start()
            threads.append(process)
        for process in threads:
            process.join()
        for bucket in buckets_results:
            if len(bucket) > 0:
                object = bucket[0]
                buckets_stats_array.extend(object)

    elif settings._THREADED == 2:
        # We can use a with statement to ensure threads are cleaned up promptly
        with concurrent.futures.ThreadPoolExecutor(max_workers=settings._MAX_THREADS) as executor:
            # Start the load operations and mark each future with its URL
            future_to_url = {executor.submit(threaded_analyse_bucket_contents, bucket_name): bucket_name for bucket_name
                             in bucket_list}
            for future in concurrent.futures.as_completed(future_to_url):
                bucket_info = future_to_url[future]
                try:
                    data, processingtime, info = future.result()
                except Exception as exc:
                    print('%r generated an exception: %s' % (bucket_info, exc))
                else:
                    buckets_stats_array.extend(data)
                    print(info)

    else:
        for bucket_name in bucket_list:

            if settings._REFRESHCACHE and os.path.isfile(bucket_name + ".cache"):
                os.remove(bucket_name + ".cache")
                if settings._VERBOSE > 1:
                    print("Bucket {} cache removed!".format(bucket_name))

            print("{0:60}".format(bucket_name), file=sys.stderr, end="\r")
            buckets.append(bucket_name)

            bucket_creation = boto3.resource("s3").Bucket(bucket_name).creation_date
            start = time.perf_counter()
            for bucket in analyse_bucket_contents(bucket_name):
                object = bucket[0]
                timing = bucket[1]
                print(
                    "{:60}{:>30}{:>20}{:>20}{:>30}{:>20}".format(bucket_name, object[0]['CreationDate'],
                                                                 object[0]['Count'],
                                                                 object[0]['Size'], object[0]['LastModified'],
                                                                 object[0]['Cost']), file=sys.stderr,
                    end="\r")
                buckets_stats_array.extend(object)
                print(
                    "{:60}{:>30}{:>20}{:>20}{:>30}{:>20}{:>40}".format(bucket_name, object[0]['CreationDate'],
                                                                       object[0]['Count'],
                                                                       object[0]['Size'], object[0]['LastModified'],
                                                                       object[0]["Cost"],
                                                                       str(timing)
                                                                       # str(timedelta(milliseconds=round(1000 * (time.perf_counter() - start))))
                                                                       ),
                    file=sys.stderr)
                if settings._VERBOSE > 1:
                    print(object)
                start = time.perf_counter()

    all_buckets_stats = {'Buckets': buckets_stats_array}
    if settings._OUTPUT_FILE.__len__() > 0:
        append_output(str(all_buckets_stats))
    if settings._VERBOSE > 0:
        print(all_buckets_stats)

    print("Grand Total:\n" \
          "  Total Buckets:   {:>40}\n" \
          "  Total Objects:   {:>40}\n" \
          "  Total Size:      {:>40}\n" \
          "  Total Cost:      {:>40}\n" \
          "  Processing Time: {:>40}"
        .format(
        len(all_buckets_stats['Buckets']),
        grand_total_objects, display_size(grand_total_size), "${:,.2f}".format(grand_total_cost),
        str(timedelta(milliseconds=round(1000 * (time.perf_counter() - realstart))))
    ), file=sys.stderr)
