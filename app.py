import os
import uuid
import threading
import time
import requests
import ffmpeg
import logging
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    after_this_request,
)
from datetime import datetime

# Configuration
app = Flask(__name__)
TEMP_DIR = "/app/temp"
os.makedirs(TEMP_DIR, exist_ok=True)

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory storage for job status (Use Redis for production/persistence)
jobs = {}
job_controls = {}  # Stores stop_events and process handles

# Frigate Authentication
frigate_auth_tokens = {}  # Cache tokens by URL


def get_frigate_auth_token(frigate_url, username=None, password=None):
    """Get JWT token from Frigate using provided or environment credentials."""
    # Check if we already have a valid token
    if frigate_url in frigate_auth_tokens:
        return frigate_auth_tokens[frigate_url]

    # Get credentials from parameters or environment
    if not username:
        username = os.getenv("FRIGATE_USERNAME")
    if not password:
        password = os.getenv("FRIGATE_PASSWORD")

    if not username or not password:
        logger.warning("No Frigate credentials provided, authentication may fail")
        return None

    try:
        login_url = f"{frigate_url.rstrip('/')}/login"
        response = requests.post(
            login_url, json={"user": username, "password": password}, timeout=10
        )
        response.raise_for_status()
        token = response.json().get("token")
        if token:
            frigate_auth_tokens[frigate_url] = token
            logger.info(f"Successfully authenticated with Frigate at {frigate_url}")
        return token
    except Exception as e:
        logger.error(f"Failed to authenticate with Frigate: {e}")
        return None


def format_timestamp(ts_str):
    """Converts local datetime string to Unix timestamp."""
    try:
        # Expected format: YYYY-MM-DDTHH:MM
        dt = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M")
        return int(dt.timestamp())
    except Exception as e:
        logger.error(f"Date parsing error: {e}")
        return None


def process_timelapse(
    job_id,
    frigate_url,
    camera,
    start_ts,
    end_ts,
    speed,
    fps,
    source_export_id=None,
    username=None,
    password=None,
):
    """Background task to download and convert video."""
    input_path = os.path.join(TEMP_DIR, f"{job_id}_input.mp4")
    output_path = os.path.join(TEMP_DIR, f"{job_id}_output.mp4")

    jobs[job_id]["status"] = "downloading"
    jobs[job_id]["progress"] = 0

    export_id = source_export_id
    should_delete_export = False  # Only delete if we created it
    stop_event = job_controls[job_id]["stop_event"]
    headers = {}  # Initialize headers for use in cleanup

    try:
        # 1. Download from Frigate
        # Clean URL
        frigate_url = frigate_url.rstrip("/")

        # Get authentication token (from parameters or environment)
        auth_token = get_frigate_auth_token(frigate_url, username, password)
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        if stop_event.is_set():
            raise Exception("Cancelled")

        if not export_id:
            # === OPTION A: Create New Export ===
            should_delete_export = True

            # 1. Trigger Export
            export_url = (
                f"{frigate_url}/api/export/{camera}/start/{start_ts}/end/{end_ts}"
            )
            logger.info(f"Job {job_id}: Requesting export from {export_url}")

            if stop_event.is_set():
                raise Exception("Cancelled")

            # Trigger export
            r_export = requests.post(export_url, json={}, headers=headers, timeout=30)
            r_export.raise_for_status()
            export_id = r_export.json().get("export_id")

            if not export_id:
                raise Exception("Frigate did not return an export ID")

            jobs[job_id]["status"] = "exporting"
            logger.info(
                f"Job {job_id}: Export started (ID: {export_id}). Waiting for completion..."
            )
        else:
            # === OPTION B: Use Existing Export ===
            logger.info(f"Job {job_id}: Using existing export {export_id}")
            # Even for existing exports, we might need to wait if it's currently in progress
            jobs[job_id]["status"] = (
                "exporting"  # Re-using this status to mean "waiting for export to be ready"
            )

        # 2. Wait for Export (or find it if existing)
        export_filename = None
        start_wait = time.time()
        while time.time() - start_wait < 600:  # 10 min timeout
            if stop_event.is_set():
                raise Exception("Cancelled")
            try:
                r_status = requests.get(
                    f"{frigate_url}/api/exports", headers=headers, timeout=10
                )
                r_status.raise_for_status()
                exports = r_status.json()

                # Find export by ID
                match = next((e for e in exports if e.get("id") == export_id), None)
                if match:
                    if match.get("in_progress") is False:
                        # Prefer video_path if available to get exact filename
                        if match.get("video_path"):
                            export_filename = os.path.basename(match.get("video_path"))
                        else:
                            export_filename = match.get("name")
                            if export_filename and not export_filename.endswith(".mp4"):
                                export_filename += ".mp4"
                        break
                    else:
                        # Export exists but is still in progress
                        pass
                elif source_export_id:
                    # We were given an ID, but it's not in the list??
                    raise Exception(f"Export {source_export_id} not found on server")

            except Exception as e:
                logger.warning(f"Error checking export status: {e}")
                if source_export_id:  # If using existing and it fails, that's bad
                    raise e

            time.sleep(2)

        if not export_filename:
            raise Exception("Export timed out or not found")

        # 3. Download
        download_url = f"{frigate_url}/exports/{export_filename}"
        jobs[job_id]["status"] = "downloading"
        logger.info(f"Job {job_id}: Downloading from {download_url}")

        if stop_event.is_set():
            raise Exception("Cancelled")

        with requests.get(download_url, stream=True, headers=headers, timeout=120) as r:
            r.raise_for_status()
            total_size = int(r.headers.get("content-length", 0))
            downloaded = 0

            with open(input_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if stop_event.is_set():
                        raise Exception("Cancelled")
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            # Cap download progress at 50% of total job
                            progress = (downloaded / total_size) * 50
                            jobs[job_id]["progress"] = int(progress)

        # 4. Delete Export from Frigate export (ONLY IF WE CREATED IT)
        if should_delete_export:
            try:
                # Use a flag to check if we are cancelling, but we still want to try to delete the export if we created it
                # Logic: If cancelled, we still want to clean up the export we started?
                # YES, if we created it, we should delete it even if cancelled.
                pass
            except Exception:
                pass

        jobs[job_id]["progress"] = 50
        jobs[job_id]["status"] = "processing"
        logger.info(f"Job {job_id}: Download complete. Starting FFmpeg.")

        if stop_event.is_set():
            raise Exception("Cancelled")

        # 2. Process with FFmpeg
        # Calculate setpts value. 1.0 is normal speed. 0.5 is 2x. 0.1 is 10x.
        # setpts = 1/speed
        pts_factor = 1.0 / float(speed)

        # Probe file to ensure it's valid
        probe = ffmpeg.probe(input_path)
        video_info = next(s for s in probe["streams"] if s["codec_type"] == "video")

        # FFmpeg command
        process = (
            ffmpeg.input(input_path)
            .filter("setpts", f"{pts_factor}*PTS")
            .output(output_path, r=fps, an=None)  # an=None removes audio
            .overwrite_output()
            .run_async(pipe_stdin=True, pipe_stdout=True, pipe_stderr=True)
        )

        # Store process for killing
        job_controls[job_id]["ffmpeg_proc"] = process

        # Wait for finish
        out, err = process.communicate()

        if process.returncode != 0:
            # Check if it was because we killed it
            if stop_event.is_set():
                raise Exception("Cancelled")
            else:
                raise Exception(f"FFmpeg error: {err}")

        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = "completed"
        jobs[job_id]["filename"] = (
            f"timelapse_{camera if camera else 'export'}_{speed}x.mp4"
        )

        # Clean up input file
        if os.path.exists(input_path):
            os.remove(input_path)

        # Clean up delete export
        if should_delete_export:
            try:
                logger.info(f"Job {job_id}: Deleting export {export_id} from Frigate")
                requests.delete(
                    f"{frigate_url}/api/export/{export_id}", headers=headers, timeout=10
                )
            except Exception as e:
                logger.warning(f"Failed to delete export {export_id} from Frigate: {e}")

    except Exception as e:
        is_cancelled = str(e) == "Cancelled"
        logger.error(
            f"Job {job_id} {'cancelled' if is_cancelled else 'failed'}: {str(e)}"
        )
        jobs[job_id]["status"] = "cancelled" if is_cancelled else "failed"
        jobs[job_id]["error"] = str(e)

        # Always try to cleanup Frigate export if we created it and failed/cancelled
        if should_delete_export and export_id:
            try:
                requests.delete(
                    f"{frigate_url}/api/export/{export_id}", headers=headers, timeout=10
                )
            except:
                pass

        # Cleanup files
        if os.path.exists(input_path):
            os.remove(input_path)
        if os.path.exists(output_path):  # Also remove output if failed
            os.remove(output_path)
    finally:
        # Cleanup controls
        job_controls.pop(job_id, None)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/cameras", methods=["POST"])
def get_cameras():
    data = request.json
    url = data.get("url", "").rstrip("/")
    username = data.get("username")
    password = data.get("password")

    logger.info(f"Connecting to Frigate at {url}")
    logger.info(
        f"Username provided: {bool(username)}, Password provided: {bool(password)}"
    )

    try:
        # Get auth token if credentials provided
        auth_token = get_frigate_auth_token(url, username, password)
        logger.info(f"Auth token obtained: {bool(auth_token)}")

        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
            logger.info("Using Bearer token for authentication")
        else:
            logger.info("No auth token, proceeding without authentication")

        # Attempt to get config to list cameras
        resp = requests.get(f"{url}/api/config", headers=headers, timeout=10)
        resp.raise_for_status()
        config = resp.json()
        cameras = list(config.get("cameras", {}).keys())
        return jsonify({"success": True, "cameras": cameras})
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 401:
            logger.error(f"Authentication failed: {e}")
            return jsonify(
                {
                    "success": False,
                    "error": "Authentication failed. Please check your Frigate username and password, or ensure the Frigate URL is correct.",
                }
            )
        logger.error(f"HTTP error: {e}")
        return jsonify({"success": False, "error": str(e)})
    except Exception as e:
        logger.error(f"Error connecting to Frigate: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/exports", methods=["POST"])
def get_exports():
    data = request.json
    url = data.get("url", "").rstrip("/")
    username = data.get("username")
    password = data.get("password")
    try:
        # Get auth token if credentials provided
        auth_token = get_frigate_auth_token(url, username, password)
        headers = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"

        resp = requests.get(f"{url}/api/exports", headers=headers, timeout=10)
        resp.raise_for_status()
        exports = resp.json()
        return jsonify({"success": True, "exports": exports})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route("/api/create", methods=["POST"])
def create_timelapse():
    data = request.json
    job_id = str(uuid.uuid4())

    start_ts = format_timestamp(data.get("startTime", ""))
    end_ts = format_timestamp(data.get("endTime", ""))
    source_export_id = data.get("exportId")

    if not source_export_id and (not start_ts or not end_ts):
        return jsonify({"error": "Invalid dates and no export ID provided"}), 400

    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "progress": 0,
        "created_at": time.time(),
    }

    job_controls[job_id] = {"stop_event": threading.Event()}

    thread = threading.Thread(
        target=process_timelapse,
        args=(
            job_id,
            data["frigateUrl"],
            data.get("camera"),
            start_ts,
            end_ts,
            int(data["speed"]),
            int(data["fps"]),
            source_export_id,
            data.get("username"),
            data.get("password"),
        ),
    )
    thread.start()

    return jsonify({"jobId": job_id})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    if job["status"] in ["completed", "failed", "cancelled"]:
        return jsonify({"status": "already_ended"})

    # Signal cancellation
    if job_id in job_controls:
        controls = job_controls[job_id]
        controls["stop_event"].set()

        # Kill FFmpeg process if running
        proc = controls.get("ffmpeg_proc")
        if proc:
            try:
                proc.kill()
            except Exception as e:
                logger.error(f"Failed to kill ffmpeg: {e}")

    job["status"] = "cancelled"
    return jsonify({"success": True})


@app.route("/download/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "completed":
        return "File not ready", 404

    path = os.path.join(TEMP_DIR, f"{job_id}_output.mp4")

    @after_this_request
    def remove_file(response):
        try:
            if os.path.exists(path):
                os.remove(path)
                jobs.pop(job_id, None)
        except Exception as e:
            logger.error(f"Error removing file: {e}")
        return response

    return send_file(path, as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
