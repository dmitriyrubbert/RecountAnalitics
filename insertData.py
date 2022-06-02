#!/usr/bin/python3
# -*- coding: utf-8 -*-
#---------------------------------------------------------------------
# Elasticsearch loading data
#
# Version      Date        Info
# 1.0          2022        Initial Version
#
# Made by Dmitriy Lazarev GOLDLINUX Copyleft (c) 2022
#---------------------------------------------------------------------
# python3 -m pip install mysql-connector elasticsearch python-dateutil

import sys, json, mysql.connector
from elasticsearch import Elasticsearch
from datetime import date, datetime, timedelta

delete_script = "TRUNCATE TABLE interaction_scripts "
delete_tags = "TRUNCATE TABLE interaction_tags" 
delete_words = "TRUNCATE TABLE interaction_words" 

get_scripts = """ SELECT pcs.id, pcs.name, pcs.dataset_id, pcs.scriptType, pcst.tag_id, pcst.script_id, pcst.isManager
    FROM phone_cdr_scripts pcs left join phone_cdr_script_tags pcst  on pcs.id  = pcst.script_id   """

get_tags = """ SELECT * FROM phone_cdr_tags WHERE NOT deleted """
get_words = """ SELECT * FROM phone_cdr_tag_words WHERE tag_id = '%s' """

add_words = ("INSERT IGNORE INTO interaction_words "
"(interactionID,word_id,counter,interactionDate,dataset_id,isManager,word_count) "
"VALUES ('{}', {}, {}, '{}', {}, {}, {})")

add_tags = ("INSERT IGNORE INTO interaction_tags "
"(interactionID,tag_id,counter,interactionDate,dataset_id,isManager) "
"VALUES ('{}', {}, {}, '{}', {}, {})")

add_scripts_soft = """   INSERT IGNORE INTO interaction_scripts ( interactionID, script_id, interactionDate, dataset_id, successful )
  SELECT interaction_tags.interactionID,
         phone_cdr_script_tags.script_id,
         interaction_tags.interactionDate,
         interaction_tags.dataset_id,
		 ( count(distinct pcsta.tag_id) = count(distinct ita.tag_id) AND count(distinct pcstc.tag_id) = count(distinct itc.tag_id) )
  FROM interaction_tags
  LEFT JOIN phone_cdr_script_tags ON interaction_tags.tag_id = phone_cdr_script_tags.tag_id
  LEFT JOIN phone_cdr_scripts ON phone_cdr_scripts.id = phone_cdr_script_tags.script_id
                             AND ifnull(phone_cdr_scripts.dataset_id,0) = ifnull(interaction_tags.dataset_id,0)
  LEFT JOIN phone_cdr_script_tags AS pcstc ON pcstc.script_id = phone_cdr_script_tags.script_id
										  AND pcstc.isManager = 0
  LEFT JOIN phone_cdr_script_tags AS pcsta ON pcsta.script_id = phone_cdr_script_tags.script_id
										  AND pcsta.isManager = 1
  LEFT JOIN interaction_tags as itc ON itc.interactionID = interaction_tags.interactionID
                                  AND itc.tag_id = pcstc.tag_id
  LEFT JOIN interaction_tags as ita ON ita.interactionID = interaction_tags.interactionID
                                  AND ita.tag_id = pcsta.tag_id
  WHERE phone_cdr_script_tags.tagType = 1
    AND phone_cdr_script_tags.tag_id IS NOT NULL
    AND phone_cdr_scripts.id IS NOT NULL
    AND phone_cdr_scripts.scriptType = "contentScript"
    AND interaction_tags.interactionDate >= '%s'
    AND interaction_tags.interactionDate <= '%s'
  	GROUP BY phone_cdr_script_tags.script_id, interaction_tags.interactionID
 """

add_scripts_hard = """  INSERT IGNORE INTO interaction_scripts ( interactionID, script_id, interactionDate, dataset_id, successful )
  SELECT interaction_tags.interactionID,
         phone_cdr_script_tags.script_id,
         interaction_tags.interactionDate,
         interaction_tags.dataset_id,
		 ( group_concat(distinct pcsta.tag_id order by pcsta.id) = group_concat(distinct ita.tag_id) AND
           group_concat(distinct pcstc.tag_id order by pcstc.id) = group_concat(distinct itc.tag_id) )
  FROM interaction_tags
  LEFT JOIN phone_cdr_script_tags ON interaction_tags.tag_id = phone_cdr_script_tags.tag_id
  LEFT JOIN phone_cdr_scripts ON phone_cdr_scripts.id = phone_cdr_script_tags.script_id
                             AND ifnull(phone_cdr_scripts.dataset_id,0) = ifnull(interaction_tags.dataset_id,0)
  LEFT JOIN phone_cdr_script_tags AS pcstc ON pcstc.script_id = phone_cdr_script_tags.script_id
										  AND pcstc.isManager = 0
  LEFT JOIN phone_cdr_script_tags AS pcsta ON pcsta.script_id = phone_cdr_script_tags.script_id
										  AND pcsta.isManager = 1
  LEFT JOIN interaction_tags as itc ON itc.interactionID = interaction_tags.interactionID
                                  AND itc.tag_id = pcstc.tag_id
  LEFT JOIN interaction_tags as ita ON ita.interactionID = interaction_tags.interactionID
                                  AND ita.tag_id = pcsta.tag_id
  WHERE phone_cdr_script_tags.tagType = 1
    AND phone_cdr_script_tags.tag_id IS NOT NULL
    AND phone_cdr_scripts.id IS NOT NULL
    AND phone_cdr_scripts.scriptType = "strictScript"
    AND interaction_tags.interactionDate >= '%s'
    AND interaction_tags.interactionDate <= '%s'
  GROUP BY phone_cdr_script_tags.script_id, interaction_tags.interactionID
  HAVING  ( group_concat(distinct pcsta.tag_id order by pcsta.id) = group_concat(distinct ita.tag_id) AND
            group_concat(distinct pcstc.tag_id order by pcstc.id) = group_concat(distinct itc.tag_id) ) IS NOT NULL
"""

load_query = ("""
    SELECT
      interactionID,
      srcAnnotation,
      dstAnnotation,
      calldate as '@timestamp',
      dataset_id,
      IF(dst = isManager, 'in', 'out') as direction
    FROM
      phone_cdr
    WHERE
        date(calldate) BETWEEN '%s' AND '%s'
    GROUP BY
      interactionID;
        """)

def fetch(query) :
    list = []
    cursor.execute(query)
    row = cursor.fetchone()
    while row is not None:
       list.append(row)
       row = cursor.fetchone()
    return list

def elastic_search(query):

    # get pit
    pit = requests.post(f'{elastic_host}/{INDEX}/_pit?keep_alive=1m').json()

    # первый запрос
    result = es.search(size=size, query=query, pit=pit, sort=sort)
    total = result['hits']['total']['value']

    if total:
        # последняя позиция
        search_after=result['hits']['hits'][-1]['sort']

        # постраничный поиск
        i=1
        while i < total:
            i+=1
            r = es.search(size=size, query=query, pit=pit, sort=sort, search_after=search_after)
            for hit in r['hits']['hits']:
                result['hits']['hits'].append( hit )
                search_after = hit['sort']

    # delete _pit
    requests.delete(f'{elastic_host}/_pit', headers={'Content-Type':'application/json'}, json={"id": pit} )

    return result

def insert(result, isManager, tag_id, word_id):

    if result['hits']['total']['value']:
        for hit in result['hits']['hits']:
            interactionID = hit["_source"]['interactionID']
            interactionDate = datetime.fromisoformat(hit["_source"]['@timestamp'])
            dataset_id = hit["_source"]['dataset_id']
            counter = 1
            word_count = 1

            if dataset_id == None or dataset_id == 'None' or dataset_id == 0:
                dataset_id = 'NULL'

            print(f"{interactionID}\t{interactionDate}\t{dataset_id}")
            cursor.execute(add_tags.format(interactionID,tag_id,counter,interactionDate,dataset_id,isManager))
            cursor.execute(add_words.format(interactionID, word_id ,counter,interactionDate,dataset_id,isManager,word_count))
            mydb.commit()

def clear_by_date(from_date, to_date):

  print("Delete data from mysql")
  # cursor.execute(delete_from_phone_cdr_temp % (from_date))
  cursor.execute(delete_tags)
  cursor.execute(delete_words)
  cursor.execute(delete_script)
  mydb.commit()

def load(index):
    days_delta = datetime.now()-timedelta(days=elconfig['load_days'])
    start_load = datetime.combine(days_delta, datetime.min.time())
    print(f"LOAD DAYS: \t{start_load}\t{to_date}\n")
    print("Load data from mysql")
    cursor.execute(load_query % (start_load, to_date))
    row = cursor.fetchone()
    while row is not None:
        exists = es.exists(index=index, id=row['interactionID'])
        if not exists:
            resp = es.index(index=index, id=row['interactionID'], document=row)
            print(resp['result'], row['interactionID'], row['@timestamp'])
        row = cursor.fetchone()

def main():
    """
    scripts(скрипты) - id.phone_cdr_scripts(имена) on script_id.phone_cdr_script_tags (данные) содержит набор маркеров
    tags(маркеры) - phone_cdr_tags содержит набор слов
     - words(слова) - phone_cdr_tag_words
    """
    
    clear_by_date(from_date, to_date)
    scripts = fetch(get_scripts)
    # {'id': 13, 'name': 'Упоминание каналов самообслуживания', 'dataset_id': 70, 'scriptType': 'strictScript', 'tag_id': 155, 'script_id': 13, 'isManager': 1}
    tags = fetch(get_tags)
    # {'id': 1, 'name': 'WOW card', 'deleted': 1, 'emotion': 4, 'class_id': 1, 'dataset_id': 70, 'tagType': 'standardTag', 'search_channel': -1}

    for tag in tags:
        tag_id = tag['id']
        dataset_id = tag['dataset_id']

        # проверка принадлежит ли маркер скрипту
        successful = 0; script_id = script_name = ''
        for script in scripts:
            if tag['id'] == script['tag_id']:
                successful = 1
                script_id = script['id']
                script_name = script['name']
        # поиск всех слов в маркере
        for word in fetch(get_words % tag_id):
            # [{'id': 8889, 'tag_id': 266, 'word': 'Один'}]
            word_id = word['id']
            word_word = word['word']


            channel = tag['search_channel']; client = 0; operator = 1; no_matters = -1
            if channel == client or channel == no_matters:
                # CLIENT(0); KQL (direction : in and srcAnnotation : "оператора")  or (direction : out and dstAnnotation :"оператора")
                isManager = 0
                templete_script="search_in_client_speech_dataset_default"
                if dataset_id != None: templete_script="search_in_client_speech_dataset_exists"

            if channel == operator or channel == no_matters:
                # OPERATOR(1); KQL (direction : in and dstAnnotation : "контакт центра") or (direction : out and srcAnnotation : "контакт центра")
                isManager = 1
                templete_script="search_in_operator_speech_dataset_default"
                if dataset_id != None: templete_script="search_in_operator_speech_dataset_exists"

            params={
                "word": word_word,
                "dataset_id": dataset_id,
                "gte": from_date.isoformat(),
                "lte": to_date.isoformat()
              }

            resp=es.search_template(index=elconfig['elastic_index'], id=templete_script, params=params)
            # print(resp)
            if resp['hits']['total']['value']: 
              print(f"\nSCRIPT: '{ script_name }'\tTAG: '{ tag['name'] }'\tWORD: '{ word['word'] }'\t MATCH: {resp['hits']['total']['value']}\n")
              insert(resp, isManager, tag_id, word_id)

    # insert scripts
    cursor.execute(add_scripts_hard % (from_date, to_date))
    # phone_cdr_temp
    cursor.callproc('add_data_phone_cdr_temp')
    mydb.commit()

    # close connection
    cursor.close()
    mydb.close()
    print("OK")

def create_index():

  with open("templates/mappings.json") as f:
        mappings = json.load(f)

  resp =  es.options(ignore_status=[400]).indices.create(
    index="q", mappings=mappings["mappings"]
    )
  # print(resp)

def put_templates(es):
  # open and load script templates from disk
  templates=[
  "search_in_client_speech_dataset_default",
  "search_in_client_speech_dataset_exists",
  "search_in_operator_speech_dataset_default",
  "search_in_operator_speech_dataset_exists"
  ]

  try:
    for tpl_name in templates:
      with open("templates/%s.json" % tpl_name) as f:
        tpl = json.load(f)
        es.put_script(id=tpl_name, script=tpl['script'])
        # print(es.get_script(id=tpl_name))
  except Exception as e:
    print(e)
    sys.exit()
  # print(es.get_script(id="search_in_client_speech_dataset_default"))

if __name__ == '__main__':

    with open("/opt/voicetech/config/mysql.conf") as f:
        config = json.load(f)
    with open("/opt/voicetech/config/elasticsearch.conf") as f:
        elconfig = json.load(f)

    if len(sys.argv) == 1:
        # load default date period from config
        days_delta = datetime.now()-timedelta(days=elconfig['recount_days'])
        from_date = datetime.combine(days_delta, datetime.min.time())
        to_date = datetime.combine(date.today(), datetime.max.time())
    elif len(sys.argv) == 3:
        # get date period from stdin
        from_date = datetime.fromisoformat(sys.argv[1])
        to_date = datetime.combine(datetime.fromisoformat(sys.argv[2]), datetime.max.time())
    else:
      print("You didn't give arguments!\
        Examle: ./loadData.py '2022-06-01' '2022-07-01'")
      sys.exit()

    print(f"RECOUNT DAYS: \t{from_date}\t{to_date}")

    mydb = mysql.connector.connect(
        host=config['host'], 
        user=config['user'], 
        passwd=config['pass'], 
        db=config['base'], 
        charset='utf8', 
        collation='utf8_general_ci' 
    )
    cursor = mydb.cursor(dictionary=True)
    es = Elasticsearch(
        elconfig['elastic_host'],
        # if use passwords
        # basic_auth=(
        #   "elastic",
        #   elconfig['elastic_password']
        #   ),
        # provide a path to CA certs on disk
        # ca_certs="ca/ca.crt",
        # no verify SSL certificates
        # verify_certs=False,
        # don't show warnings about ssl certs verification
        # ssl_show_warn=False
    )

    create_index()
    put_templates(es)
    load(elconfig['elastic_index'])
    main()