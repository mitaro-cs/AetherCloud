# AetherCloud

AetherCloud is a Flask-based cloud storage app with:
- registration/login/reset password
- personal folder tree
- personal root folder named by user profile name
- file upload/download/delete
- file thumbnails/previews (images + type icons)
- user settings (profile + password change)
- admin-only console for `miri.saro@bk.ru`
- storage usage meter and limits
- deep black dashboard UI with white gradient accents

The app keeps auth pages in the original visual direction and adds a full internal cloud workspace at `/cloud`.

## What is implemented

- Auth:
  - `GET/POST /` - registration
  - `GET/POST /login` - login
  - `GET/POST /forgot` - password reset
  - `GET /logout` - sign out
- Cloud:
  - `GET /cloud` - workspace view
    - modes: `home`, `storage`, `sync`, `stats`, `settings` (query param `view`)
    - sidebar tabs: `folders` and `tags` (query param `tab`)
  - `POST /cloud/folders` - create folder
  - `POST /cloud/folders/<folder_id>/delete` - delete folder (recursive)
  - `POST /cloud/files/upload` - upload multiple files or an entire folder (with nested structure) into current folder
  - `GET /cloud/files/<file_id>/download` - download file
  - `GET /cloud/files/<file_id>/preview` - thumbnail/preview source
  - `POST /cloud/files/<file_id>/delete` - delete file
  - `POST /cloud/sync/check` - run consistency check
  - `POST /cloud/settings/profile` - update display name
  - `POST /cloud/settings/password` - change password
- Admin:
  - `GET /admin` - admin console (accessible only for `miri.saro@bk.ru`)
  - shows global stats and all users overview
- Storage logic:
  - SQLite metadata (users/folders/files)
  - physical file storage by user and folder
  - shared storage pool split equally between all users
  - max request size check (`AETHER_MAX_UPLOAD_MB`)
- UI:
  - left icon rail + folder tree panel + workspace area
  - folder cards and files table
  - breadcrumbs and storage usage progress
  - responsive behavior for desktop/mobile

## Project file

- `AetherCloud.py` - backend + templates + styles (single-file app)

## Requirements

- Python 3.10+ (tested with Python 3.13)
- pip

Install:

```powershell
pip install flask werkzeug
```

## Configuration (important for external disk)

You can place both DB and storage on another disk via environment variables.

- `AETHER_DB_PATH` - path to SQLite file
- `AETHER_STORAGE_DIR` - path to binary storage folder
- `AETHER_SECRET_KEY` - Flask session secret
- `AETHER_TOTAL_STORAGE_GB` - total shared storage in GB (default: `280`)
- `AETHER_MAX_UPLOAD_MB` - max upload size in MB (default: `250`)
- `AETHER_HOST` - bind host (default: `127.0.0.1`)
- `AETHER_PORT` - app port (default: `5000`)
- `AETHER_TUNA_URL` - your public tuna URL (optional, printed on startup)

Example (Windows, external `E:` disk):

```powershell
$env:AETHER_DB_PATH = "E:\\AetherCloudData\\users.db"
$env:AETHER_STORAGE_DIR = "E:\\AetherCloudData\\storage"
$env:AETHER_SECRET_KEY = "change-this-secret"
$env:AETHER_PORT = "5000"
$env:AETHER_TUNA_URL = "https://your-subdomain.tuna.am"
python AetherCloud.py
```

## Run

```powershell
python AetherCloud.py
```

Open:

- `http://127.0.0.1:5000/`
- your `AETHER_TUNA_URL` if set

## Data model

SQLite tables:

- `users(id, fullname, email, password, created)`
- `folders(id, user_id, parent_id, name, created)`
- `files(id, user_id, folder_id, original_name, stored_name, size, mime_type, uploaded)`

Notes:
- Legacy compatibility is included for old `files` schema.
- First login/registration creates only one root folder and it is named from user profile.

## Notes on storage cleanup

You requested deletion of `storage` and DB files from the project folder.
They are removed.  
On next app start they will be created again automatically if paths point to this folder.

To avoid local recreation in the project root, set:
- `AETHER_DB_PATH`
- `AETHER_STORAGE_DIR`

to a different disk/folder before launch.
