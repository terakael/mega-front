import base64
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import sqlite3
import subprocess
from flask import (
    Blueprint,
    Flask,
    send_file,
    render_template,
    send_from_directory,
    request,
    Response,
)

# from dotenv import load_dotenv
from lib.encrypt import Encrypter

with open("/etc/secrets/kek/key", "r") as f:
    key = f.read()

with open("/etc/secrets/kek/iv", "r") as f:
    iv = f.read()

app = Flask(__name__)

bp = Blueprint("homevids", __name__)

encrypter = Encrypter(key=base64.b64decode(key), iv=base64.b64decode(iv))

db_file = os.path.join(app.static_folder, "database_dan.db")


@bp.route("/video/<path:filename>")
def video(filename):
    print(f"pulling next file: {filename}")

    raw_filestem = os.path.splitext(filename.replace("_part0", ""))[0]

    # if exists on disk, send it; else download from server
    cached_file_location = os.path.join(app.static_folder, "cache", raw_filestem)
    if os.path.exists(os.path.join(cached_file_location, filename)):
        print(f"pulled file {filename} from cache")
        return send_from_directory(cached_file_location, filename)

    # Handle range requests for streaming
    range_header = request.headers.get("Range")
    file_size = None  # Will need to get actual size if possible

    # Get streaming response from download_from_server
    response = Response(
        download_from_server(filename),
        status=206 if range_header else 200,
        mimetype=(
            "application/x-mpegURL" if filename.endswith(".m3u8") else "video/MP2T"
        ),
        direct_passthrough=True,
    )

    response.headers["Accept-Ranges"] = "bytes"
    if range_header and file_size:
        response.headers["Content-Range"] = f"bytes 0-{file_size-1}/{file_size}"

    return response


def _images_including_people(cursor, selected_names, per_page, offset):
    query = f"""
        SELECT v.filename, v.timestamp
        FROM video v
        JOIN video_people vp ON v.id = vp.video_id
        JOIN people p ON vp.people_id = p.id
        WHERE p.name IN ({', '.join('?' for _ in selected_names)})
        GROUP BY v.id
        HAVING COUNT(DISTINCT p.id) = ?
    """
    cursor.execute(
        f"{query} ORDER BY v.timestamp DESC LIMIT ? OFFSET ?",
        (*selected_names, len(selected_names), per_page, offset),
    )
    images = cursor.fetchall()

    cursor.execute(
        f"select count(*) from ({query})", (*selected_names, len(selected_names))
    )
    count = cursor.fetchone()[0]

    return images, count


def _images_with_only_people(cursor, selected_names, per_page, offset):
    name_placeholder = ", ".join("?" for _ in selected_names)

    cursor.execute(
        f"select id from people where name in ({name_placeholder})", selected_names
    )
    selected_ids = [row[0] for row in cursor.fetchall()]

    query = f"""
        WITH people_videos AS (
            SELECT video_id
            FROM video_people
            WHERE people_id IN ({name_placeholder})
            GROUP BY video_id
            HAVING COUNT(DISTINCT people_id) = (SELECT COUNT(*) FROM people WHERE id IN ({name_placeholder}))
        ),
        exact_match_videos AS (
            SELECT vp.video_id
            FROM video_people vp
            JOIN people_videos pv ON vp.video_id = pv.video_id
            GROUP BY vp.video_id
            HAVING COUNT(DISTINCT vp.people_id) = (SELECT COUNT(*) FROM people WHERE id IN ({name_placeholder}))
        )
        SELECT v.filename, v.timestamp
        FROM video v
        JOIN exact_match_videos emv ON v.id = emv.video_id
    """

    cursor.execute(
        f"{query} ORDER BY v.timestamp DESC LIMIT ? OFFSET ?",
        (*selected_ids, *selected_ids, *selected_ids, per_page, offset),
    )
    images = cursor.fetchall()

    cursor.execute(
        f"select count(*) from ({query})", (*selected_ids, *selected_ids, *selected_ids)
    )
    count = cursor.fetchone()[0]

    return images, count


@bp.route("/", methods=["GET", "POST"])
def index():
    print(request.form.to_dict())
    selected_names = [
        name for name in request.form.get("selected_name_list", "").split(",") if name
    ]
    page = int(request.form.get("page", 1))

    per_page = 20
    offset = (page - 1) * per_page

    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    if selected_names:
        if "Only" in selected_names:
            images, total_images = _images_with_only_people(
                cursor, [x for x in selected_names if x != "Only"], per_page, offset
            )
        else:
            images, total_images = _images_including_people(
                cursor, selected_names, per_page, offset
            )
    else:
        cursor.execute(
            "SELECT filename, timestamp FROM video order by timestamp desc LIMIT ? OFFSET ?",
            (per_page, offset),
        )
        images = cursor.fetchall()

        cursor.execute("SELECT COUNT(*) FROM video")
        total_images = cursor.fetchone()[0]

    total_pages = (total_images // per_page) + (
        1 if total_images % per_page != 0 else 0
    )

    cursor.execute("SELECT name from people")
    people = ["Only"] + [person[0] for person in cursor.fetchall()]

    conn.close()

    return render_template(
        "index.html",
        images=images,
        page=page,
        total_pages=total_pages,
        people=people,
        selected_names=",".join(selected_names),
    )


@bp.route("/<path:filename>")
def videos(filename):
    return render_template("video.html", filestem=os.path.splitext(filename)[0])


def trace_callback(query):
    print("executing query: ", query)


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


def generate_upload_path(server_dek, timestamp, video_filestem):
    # on mega, video contents is stored in the directory:
    # /year_hash/month_hash/day_hash/video_filestem_hash/
    # note that files that make up the video contents aren't necessarily
    # stored on the same server.
    # year/month/day hashes are hashed based on the server's email as well as kek
    # so that each server has different folder names.

    hash_section = lambda x: encrypter.hash(x, iv=server_dek)[:6]

    dt = datetime.fromtimestamp(timestamp, tz=timezone(timedelta(hours=9)))
    return os.path.join(
        *[
            hash_section(f"{dt.year}"),
            hash_section(f"{dt.year}/{dt.month}"),
            hash_section(
                f"{dt.year}/{dt.month}/{dt.day}"
            ),  # don't want the month/day hashes to match within multiple year directories
            hash_section(video_filestem),
        ]
    )


def download_from_server(filename):
    if filename.endswith(".m3u8"):
        file_stem = os.path.splitext(filename)[0]
        server_name, server_password, server_dek, timestamp, video_dek = db_fetch(
            """
                SELECT s.email, s.mega_pw, s.dek, v."timestamp", v.dek
                FROM server s
                JOIN playlist p ON p.server_id = s.id
                JOIN video v ON p.video_id = v.id
                WHERE v.filename = ?;
            """,
            (file_stem,),
            fetch_type="one",
        )

    elif filename.endswith(".ts"):
        file_stem, chunk_id = re.match(r"(.+)_part(\d+)\.ts", filename).groups()
        server_name, server_password, server_dek, timestamp, video_dek = db_fetch(
            """
                SELECT s.email, s.mega_pw, s.dek, v."timestamp", v.dek
                FROM server s
                JOIN video_chunk vc ON vc.server_id = s.id
                JOIN video v ON vc.video_id = v.id
                WHERE v.filename = ? AND vc.chunk_id = ?;
            """,
            (file_stem, chunk_id),
            fetch_type="one",
        )

    server_dek = encrypter.decrypt(server_dek)
    video_dek = encrypter.decrypt(video_dek)

    filename_hash = encrypter.hash(filename, iv=video_dek)
    upload_path = generate_upload_path(server_dek, timestamp, file_stem)

    command = [
        "megatools",
        "get",
        "--username",
        server_name,
        "--password",
        encrypter.decrypt(server_password, iv=server_dek).decode("utf-8"),
        "--path",
        "-",
        f"/Root/{os.path.join(upload_path, filename_hash)}",
    ]

    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=False)
    decryptor = encrypter.cipher(video_dek).decryptor()

    def generate():
        try:
            while True:
                chunk = process.stdout.read(4096)  # Read in 4KB chunks
                if not chunk:
                    yield decryptor.finalize()
                    break
                yield decryptor.update(chunk)
        finally:
            process.kill()

    return generate()


app.register_blueprint(bp)
if __name__ == "__main__":
    app.run(debug=True)
