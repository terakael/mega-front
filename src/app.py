import os
import sqlite3
import base64
import subprocess
from datetime import datetime
from flask import (
    Flask,
    request,
    jsonify,
    Response,
    render_template,
)
from common.encrypt import Encrypter
from dotenv import load_dotenv
from flask_cors import CORS
import json

app = Flask(__name__)
CORS(app)
load_dotenv()

mega_root = os.getenv("mega_root")
db_file = os.path.join(mega_root, "database.db")
encrypter = Encrypter(
    key=base64.b64decode(os.getenv("key")), iv=base64.b64decode(os.getenv("iv"))
)


def trace_callback(query):
    print("executing query: ", query)


thumbnails_by_date = {}


def db_fetch(query, parameters=(), *, fetch_type="all", fetch_count=None):
    with sqlite3.connect(db_file) as conn:
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(trace_callback)
        cursor = conn.cursor()
        cursor.execute(query, parameters)

        match fetch_type:
            case "all":
                return cursor.fetchall()
            case "one":
                return cursor.fetchone()
            case "many":
                return cursor.fetchmany(fetch_count)


def setup_caches():
    records = db_fetch(
        """
        SELECT 
            strftime('%Y-%m-%d', unix_timestamp, 'unixepoch', 'localtime', '+9 hours') AS date, 
            COUNT(*) AS count 
        FROM 
            v_distinct_files 
        GROUP BY 
            date;
    """
    )

    for record in records:
        dt = datetime.strptime(record["date"], "%Y-%m-%d")
        if dt.year not in thumbnails_by_date:
            thumbnails_by_date[dt.year] = []
        thumbnails_by_date[dt.year].append({record["date"]: record["count"]})


setup_caches()


@app.route("/", methods=("GET",))
def index():
    return render_template("thumbnails.html")


@app.route("/distinct_years", methods=("GET",))
def distinct_years():
    ret = json.dumps(list(thumbnails_by_date.keys()))
    print(ret)
    return ret


@app.route("/files_by_day", methods=("GET",))
def files_by_day():
    year = int(request.args.get("year"))
    return json.dumps({year: thumbnails_by_date[year]})


@app.route("/thumbnails", methods=("GET",))
def thumbnails():
    target_date = request.args.get("targetDate")
    offset = request.args.get("fromIndex")
    limit = request.args.get("limit")

    # TODO input validation

    records = db_fetch(
        """
        select 
            filename,
            strftime('%Y-%m-%d', unix_timestamp, 'unixepoch', 'localtime', '+9 hours') as date
        from 
            v_distinct_files 
        WHERE unix_timestamp <= strftime('%s', ? || ' 00:00:00 -09:00')
        order by unix_timestamp DESC
        limit ? offset ?;""",
        (target_date, limit, offset),
    )

    images = []
    for record in records:
        filepath = os.path.join(
            app.static_folder, "thumbnails", f"{record['filename']}.jpg"
        )
        with open(filepath, "rb") as image_file:
            b64 = base64.b64encode(image_file.read()).decode("utf-8")

        images.append([os.path.basename(filepath)[:-4], b64])

    return jsonify(images), 200


@app.route("/stream", methods=("GET",))
def data():
    filename = request.args.get("filename")
    chunk = request.args.get("chunkIndex")
    if chunk:  # if there's a chunk index then it's a video
        filename = filename.replace(".webm", f"_{chunk.zfill(4)}.webm")

    placeholder = request.args.get("placeholder")

    mimetype = "video/webm" if filename.endswith("webm") else "image/jpeg"
    if filename.endswith("_0000.webm") or (filename.endswith(".jpg") and placeholder):
        return Response(download_from_disk(filename), mimetype=mimetype)

    record = db_fetch(
        """
            select 
                filename, 
                files.email, 
                mega_pw, 
                servers.dek as server_dek, 
                files.dek as data_dek
            from files
            inner join servers on servers.email = files.email
            where filename=?
        """,
        (filename,),
        fetch_type="one",
    )

    if not record:
        return Response(status=204)

    return Response(download_from_server(record), mimetype=mimetype)


@app.after_request
def after_request(response):
    response.headers.add("Accept-Ranges", "bytes")
    return response


def get_chunk(byte1=None, byte2=None):
    # full_path = "try2.mp4"
    filename = "20240505_075545.mp40.ts"
    # chunk = request.args.get('chunkIndex')

    # filename = filename.replace('.webm', f'_{chunk.zfill(4)}.webm')
    full_path = os.path.join(app.static_folder, filename)
    file_size = os.stat(full_path).st_size
    start = 0

    if byte1 < file_size:
        start = byte1
    if byte2:
        length = byte2 + 1 - byte1
    else:
        length = file_size - start

    with open(full_path, "rb") as f:
        f.seek(start)
        chunk = f.read(length)
    return chunk, start, length, file_size


import re


@app.route("/stream_test")
def get_file():
    range_header = request.headers.get("Range", None)
    byte1, byte2 = 0, None
    if range_header:
        match = re.search(r"(\d+)-(\d*)", range_header)
        groups = match.groups()

        if groups[0]:
            byte1 = int(groups[0])
        if groups[1]:
            byte2 = int(groups[1])

    chunk, start, length, file_size = get_chunk(byte1, byte2)
    resp = Response(
        chunk,
        206,
        mimetype="video/mp4",
        content_type="video/mp4",
        direct_passthrough=True,
    )
    resp.headers.add(
        "Content-Range",
        "bytes {0}-{1}/{2}".format(start, start + length - 1, file_size),
    )
    return resp


def download_from_disk(filename):
    # the first chunk of the video is cached on disk for quick viewing
    with open(os.path.join(app.static_folder, "previews", filename), "rb") as f:
        while chunk := f.read(4096):
            yield chunk


def download_from_server(db_entry):
    server_dek = encrypter.decrypt(db_entry["server_dek"])
    data_dek = encrypter.decrypt(db_entry["data_dek"])
    filename_hash = encrypter.hash(db_entry["filename"], data_dek)

    command = [
        "megatools",
        "get",
        # '--dryrun',
        # '--no-progress',
        "--username",
        db_entry["email"],
        "--password",
        encrypter.decrypt(db_entry["mega_pw"], iv=server_dek).decode("utf-8"),
        "--path",
        "-",
        f"/Root/{filename_hash}",
    ]

    with subprocess.Popen(command, stdout=subprocess.PIPE, text=False) as process:
        decryptor = encrypter.cipher(data_dek).decryptor()
        while True:
            line = process.stdout.read(1024)
            if not line:
                yield decryptor.finalize()
                break
            decrypted = decryptor.update(line)
            yield decrypted


if __name__ == "__main__":
    app.run(debug=True, threaded=True)
