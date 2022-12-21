import datetime
import json
import os
import shutil
import airflow
import gcloud
import pandas as pd
import requests
from airflow.models import Variable
from airflow.operators.dummy_operator import DummyOperator
from airflow.operators.python_operator import PythonOperator
from gcloud import storage
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

YESTERDAY = datetime.datetime.now() - datetime.timedelta(days=1)
CRONTAB = "0 0 * * *" # Isso significa que será executada todos os dias à meia noite. (Você pode alterar isso, caso deseje!)
TAGS_LIST = ['Spotify', 'ETL', 'Demonstration']

# Utilizaremos uma variável de ambiente chamada "spotify_etl_dag_vars" para armazenar o Token do Spotify. Portanto precisamos ler ela:
spotify_etl_config = Variable.get("spotify_etl_dag_vars", deserialize_json=True)
SPOTIFY_SECRET = spotify_etl_config["spotify_secret"]
BUCKET = 'poc_etl' # O nome do bucket que utilizamos para armazenar os arquivos com os dados das músicas.

default_args = {
    'owner': 'Willian de Vargas', # Coloque seu nome
    'start_date': YESTERDAY,
}

# Definição de todas as funções que vamos utilizar (teremos pequenas modificações em relação às funções utilizadas na primeira etapa deste tutorial, por isso não vamos realizar a importação delas. Mas poderiamos apenas importar elas de um arquivo externo, se quisessemos)

def upload_object_to_bucket(bucket_name: str, object_path: str, object_key=None) -> bool:
    """
    Esta função é responsável por fazer upload de um arquivo/objeto a um determinado bucket no GCS.
    :param str bucket_name: Nome do bucket que receberá o arquivo/objeto.
    :param str object_path: Caminho local para o arquivo/objeto que será enviado ao bucket.
    :param str object_key: O caminho final do objeto dentro do bucket. Este parametro é opcional e caso não seja informado o valor do parametro object_path será assumido.
    :returns: True se o upload for bem sucedido. False caso contrário.
    :rtype: bool
    """

    file_name = (object_path if (object_key==None) else object_key)
        
    try:
        # Instanciando um novo cliente da API gcloud
        client = storage.Client()
        # Recuperando um objeto referente ao nosso Bucket
        bucket = client.get_bucket(bucket_name)
        if bucket==None:
            return False
        # Fazendo upload do objeto (arquivo) desejado
        blob = bucket.blob(file_name)
        blob.upload_from_filename(object_path)
    except Exception as e:
        print(e)
        return False
    except FileNotFoundError as e:
        print(e)
        return False
        
    return True

def extract_spotify_data(bucket_name='') -> str:
    
    # Definindo os headers para a requisição à API
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SPOTIFY_SECRET}"
    }

    # Definindo um limite de 24 horas antes do momento que a função for executada
    # Com isso, conseguimos recuperar as últimas 50 músicas reproduzidas nas últimas  24 horas
    today = datetime.datetime.now()
    yesterday = today - datetime.timedelta(days=1) # Ontem = Hoje - 1 Dia
    # Convertendo a data para o formato Unix Timestamp, que é o formato utilizado pela API
    yesterdays_timestamp = int(yesterday.timestamp())*1000

    # Realizando a requisição: 
    # O parâmetro "after" serve para indicarmos a partir de quando devemos fazer a busca
    # O parâmetro "limit" define o limite de músicas retornadas (o valor máximo é 50)
    request = requests.get(f"https://api.spotify.com/v1/me/player/recently-played?after={yesterdays_timestamp}&limit=50", headers = headers)

    # Transformando o resultado da requisição em um objeto JSON
    data = request.json()

    # Recuperando a data e hora da execução
    date = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    # Checando a existência do diretório local para armazenar o JSON
    if not os.path.exists('/home/airflow/spotify_data/raw/'):
        os.makedirs('/home/airflow/spotify_data/raw/')

    # Salvando o resultado da requisição em um arquivo JSON 
    file_name = f'/home/airflow/spotify_data/raw/{date}_spotify_data.json'
    with open(file_name, 'w+') as f:
        json.dump(data, f, indent=4)

    # Fazendo upload do arquivo JSON para o bucket
    object_key = f"raw/{date}_spotify_data.json"
    upload_object_to_bucket(bucket_name=bucket_name, object_path=file_name, object_key=object_key)

    # Retorna o object key do objeto gerado (JSON com as músicas) dentro do bucket
    full_object_key = f"{bucket_name}/{object_key}"
    return full_object_key

def transform(ds, **kwargs) -> str:
    # Recuperando o retorno da função anterior
    ti = kwargs['ti']
    json_object_key = ti.xcom_pull(task_ids='extract_data_from_spotify_api')

    # Fazendo download do JSON do bucket, para iniciar as transformações
    try:
        # Instanciando um novo cliente da API gcloud
        client = storage.Client()

        # Variáveis auxiliares
        full_object_key_splited = json_object_key.split('/')
        bucket_name = full_object_key_splited[0]
        object_key = json_object_key.replace(f"{bucket_name}/", "")
        local_path = f"/home/airflow/spotify_data/raw/{full_object_key_splited[-1]}"
        
        print(f"LOCAL PATH: {local_path}")

        # Criando um objeto para o Bucket
        bucket = client.get_bucket(bucket_name)
        # Criando um objeto BLOB para o caminho do arquivo
        blob = bucket.blob(object_key)
        # Fazendo download local do arquivo
        blob.download_to_filename(local_path)
    except Exception as e:
        print(e)
        exit()

    # Abrindo o JSON que foi baixado
    file = open(local_path)
    data = json.load(file)
    file.close()
    
    # Definindo as listas que vão armazenar as informações que desejamos. 
    # Elas irão nos auxiliar a compor o Dataframe final que vai resultar num .csv.
    song_names = []
    album_names = []
    artist_names = []
    songs_duration_ms = []
    songs_popularity = []
    played_at_list = []

    # Percorrendo todos os itens presentes no JSON e capturando as informações que
    # queremos armazenar no .csv final
    for song in data["items"]:
        song_names.append(song["track"]["name"])
        album_names.append(song["track"]["album"]["name"])
        artist_names.append(song["track"]["album"]["artists"][0]["name"])
        songs_duration_ms.append(song["track"]["duration_ms"])
        songs_popularity.append(song["track"]["popularity"])
        played_at_list.append(song["played_at"])

    # Criando um dicionário com os resultados obtidos nas listas
    song_dict = {
        "song_name": song_names,
        "album_name": album_names,
        "artist_name": artist_names,
        "duration_ms": songs_duration_ms,
        "popularity": songs_popularity,
        "played_at": played_at_list
    }

    # Transformando nosso dicionário em um dataframe
    song_df = pd.DataFrame(song_dict, columns=["song_name", "album_name", "artist_name", "duration_ms", "popularity", "played_at"])
    
    # Checando a existência do diretório local para armazenar o .csv
    if not os.path.exists('/home/airflow/spotify_data/transformed/'):
        os.makedirs('/home/airflow/spotify_data/transformed/')

    # Convertendo nosso dataframe para um .csv
    file_name = ((full_object_key_splited[-1]).rsplit('.',1)[0])+'.csv'
    local_path = f"/home/airflow/spotify_data/transformed/{file_name}"
    song_df.to_csv(local_path, index=False)

    # Fazendo upload do arquivo JSON para o bucket
    object_key = f"transformed/{file_name}"
    upload_object_to_bucket(bucket_name=bucket_name, object_path=local_path, object_key=object_key)
    
    # Removendo os arquivos locais gerados
    shutil.rmtree("/home/airflow/spotify_data/")

    # Retorna o object key do objeto gerado (JSON com as músicas) dentro do bucket
    full_object_key = f"{bucket_name}/{object_key}"
    return full_object_key

def load(ds, **kwargs) -> bool:
    # Recuperando o retorno da função anterior
    ti = kwargs['ti']
    csv_object_key = ti.xcom_pull(task_ids='transform_data')

    # Instanciando um novo cliente da API gcloud
    client = bigquery.Client()

    # Checa se o dataset existe. Se não existe, cria um novo dataset
    dataset_id = "Spotify_Data"
    try:
        client.get_dataset(dataset_id)
        print(f"O dataset {dataset_id} já existe!")
    except NotFound:
        try:
            dataset = bigquery.Dataset(f"{client.project}.Spotify_Data")
            client.create_dataset(dataset, timeout=30)
            print(f"Dataset '{dataset_id}' criado com sucesso!")
        except Exception as e:
            print(e)
            return False
    
    try:
    
        # Definindo nova tabela
        table_id = f"{client.project}.{dataset_id}.recently_played"

        destination_table = client.get_table(table_id)
        
        # Definindo a configuração do Job
        job_config = bigquery.LoadJobConfig(
            # Definições do nosso schema (estrutura da tabela)
            schema=[
                bigquery.SchemaField("song_name", "STRING"),
                bigquery.SchemaField("album_name", "STRING"),
                bigquery.SchemaField("artist_name", "STRING"),
                bigquery.SchemaField("duration_ms", "INTEGER"),
                bigquery.SchemaField("popularity", "INTEGER"),
                bigquery.SchemaField("played_at", "TIMESTAMP")
            ],
            # Aqui definimos o número de linhas que queremos pular.
            # Como a primeira linha do nosso csv contém o nome das colunas, queremos pular sempre 1 linha
            skip_leading_rows = 1,
            # Aqui definimos o formato do arquivo fonte (o nosso é um .csv)
            source_format=bigquery.SourceFormat.CSV,
        )

        # Capturando o número de registros na tabela antes de iniciar o load
        query_count = f"SELECT COUNT(*) FROM {table_id}"
        query_count_job = client.query(query_count)
        start_count = 0
        for row in query_count_job:
            start_count=start_count+row[0]

        # Definimos a URI do nosso objeto .csv transformado dentro do bucket
        print(f"OBJECT KEY: {csv_object_key}")
        uri = f"gs://{csv_object_key}"

        # Iniciamos o job que vai carregar os dados para dentro da nossa tabela no BigQuery
        load_job = client.load_table_from_uri(
            uri, table_id, job_config=job_config
        )

        load_job.result()

        # Removendo duplicatas
        remove_duplicates_query = ( f"CREATE OR REPLACE TABLE {table_id}"
                                    f" AS (SELECT DISTINCT * FROM {table_id})")
        remove_duplicates_job = client.query(remove_duplicates_query)
        remove_duplicates_job.result()
        # Capturando o número de registros na tabela depois de realizar o load
        query_count = f"SELECT COUNT(*) FROM {table_id}"
        query_count_job = client.query(query_count)
        end_count = 0
        for row in query_count_job:
            end_count=end_count+row[0]   

        print(f"{end_count-start_count} novos registros em {table_id}!")
    except Exception as e:
        print(e)
        return False
    
    return True

# Definição da DAG (vamos definir a execução desta dag)

with airflow.DAG('spotify_etl_dag', schedule_interval=CRONTAB, tags=TAGS_LIST, default_args=default_args, catchup=False) as dag:

    begin = DummyOperator(
        task_id='begin'
    )

    extraction = PythonOperator(
        task_id = 'extract_data_from_spotify_api',
        python_callable = extract_spotify_data,
        op_kwargs={"bucket_name":BUCKET}
    )

    transformation = PythonOperator(
        task_id = 'transform_data',
        python_callable = transform,
    )

    load_data_into_bq = PythonOperator(
        task_id = 'load_data_into_bigquery',
        python_callable = load,
    )

    end = DummyOperator(
        task_id='end',
        trigger_rule='none_failed'
    )

    # Declaring dependences between tasks
    begin >> extraction >> transformation >> load_data_into_bq >> end