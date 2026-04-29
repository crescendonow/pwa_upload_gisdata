# PWA Upload Credentials

Do not commit real Google service account keys to Git.

## Local development

Create a local `.env` file from `.env.example` and replace the placeholder values:

```env
DRIVE_ROOT_FOLDER_ID="your-google-drive-folder-id"
GOOGLE_SERVICE_ACCOUNT_JSON='paste-service-account-json-here'
PORT=8000
```

## Deployment

Set `DRIVE_ROOT_FOLDER_ID` and `GOOGLE_SERVICE_ACCOUNT_JSON` in the deployment platform's secret or environment variable settings.

If a service account key was committed or exposed, revoke that key in Google Cloud Console and create a new one.
