from etl import extract_spotify_data, transform, load

if __name__=='__main__':

    json_path = extract_spotify_data(spotify_secret_file_path="secrets/spotify_secrets.json", bucket_name="spotify_etl_dataproc")
    transformation = transform(json_object_key=json_path)
    load_result = load(transformation)
    print(load_result)