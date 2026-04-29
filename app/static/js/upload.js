const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const CHUNK_SIZE = 8 * 1024 * 1024;
const CONCURRENCY = 3;
const STORAGE_PREFIX = 'pwa-drive-upload:v1:';

const regionInput = document.getElementById('region');
const branchInput = document.getElementById('branchId');
const assetInput = document.getElementById('assetType');
const fileTypeInput = document.getElementById('fileType');
const fileInput = document.getElementById('fileInput');
const folderInput = document.getElementById('folderInput');
const dropzone = document.getElementById('dropzone');
const uploadBtn = document.getElementById('uploadBtn');
const clearBtn = document.getElementById('clearBtn');
const fileList = document.getElementById('fileList');
const fileCount = document.getElementById('fileCount');
const totalSize = document.getElementById('totalSize');
const overallPercent = document.getElementById('overallPercent');
const overallBar = document.getElementById('overallBar');
const backendStatus = document.getElementById('backendStatus');
const driveStatus = document.getElementById('driveStatus');
const message = document.getElementById('message');

let items = [];
let isUploading = false;

checkHealth();
render();

fileInput.addEventListener('change', () => {
  addFiles(Array.from(fileInput.files || []));
  fileInput.value = '';
});

folderInput.addEventListener('change', () => {
  addFiles(Array.from(folderInput.files || []));
  folderInput.value = '';
});

uploadBtn.addEventListener('click', () => {
  uploadAll().catch((error) => showMessage(error.message || 'Upload failed', 'error'));
});

clearBtn.addEventListener('click', () => {
  if (isUploading) return;
  items = [];
  showMessage('', '');
  render();
});

branchInput.addEventListener('input', () => {
  branchInput.value = branchInput.value.replace(/\D/g, '').slice(0, 7);
  render();
});

for (const input of [regionInput, assetInput, fileTypeInput]) {
  input.addEventListener('change', render);
}

for (const eventName of ['dragenter', 'dragover']) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.add('active');
  });
}

for (const eventName of ['dragleave', 'drop']) {
  dropzone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropzone.classList.remove('active');
  });
}

dropzone.addEventListener('drop', async (event) => {
  const files = await getDroppedFiles(event.dataTransfer);
  addFiles(files);
});

function addFiles(files) {
  const nextItems = files
    .filter((file) => file && file.size >= 0)
    .map((file) => ({
      id: crypto.randomUUID(),
      file,
      relativePath: normalizeRelativePath(file.webkitRelativePath || file.relativePath || file.name),
      status: 'Ready',
      state: 'ready',
      progress: 0,
      uploadedBytes: 0,
      uploadSize: file.size,
      uploadRelativePath: '',
      destinationPath: '',
      link: '',
      error: '',
    }));

  items = items.concat(nextItems);
  render();
}

async function uploadAll() {
  if (isUploading) return;

  const validationError = validateDestination();
  if (validationError) {
    showMessage(validationError, 'error');
    return;
  }

  const uploadItems = items.filter((item) => item.state === 'ready' || item.state === 'error');
  if (!uploadItems.length) return;

  isUploading = true;
  showMessage('', '');
  uploadItems.forEach((item) => {
    item.state = 'working';
    item.status = 'Queued';
    item.error = '';
    item.progress = 0;
    item.uploadedBytes = 0;
  });
  render();

  let cursor = 0;
  const workers = Array.from({ length: Math.min(CONCURRENCY, uploadItems.length) }, async () => {
    while (cursor < uploadItems.length) {
      const item = uploadItems[cursor];
      cursor += 1;
      await uploadOne(item);
    }
  });

  try {
    await Promise.all(workers);
    const failures = items.filter((item) => item.state === 'error').length;
    showMessage(failures ? `${failures} file(s) failed. Uploaded files remain in Google Drive.` : 'Upload complete.', failures ? 'warn' : 'ok');
  } finally {
    isUploading = false;
    render();
    checkHealth();
  }
}

async function uploadOne(item) {
  try {
    item.status = 'Preparing';
    render();

    const prepared = await prepareFile(item.file, item.relativePath);
    item.uploadSize = prepared.blob.size;
    item.uploadRelativePath = prepared.relativePath;

    const storageKey = buildStorageKey(item, prepared);
    let session = readStoredSession(storageKey);
    let startByte = 0;

    if (session && session.uploadUrl) {
      item.status = 'Checking resume';
      render();
      try {
        const status = await queryUploadStatus(session.uploadUrl, prepared.blob.size);
        if (status.done) {
          markDone(item, status.body || {}, prepared.blob.size);
          localStorage.removeItem(storageKey);
          return;
        }
        startByte = status.nextByte;
      } catch (error) {
        localStorage.removeItem(storageKey);
        session = null;
      }
    }

    if (!session) {
      item.status = 'Creating session';
      render();
      session = await createUploadSession(prepared);
      localStorage.setItem(storageKey, JSON.stringify({
        uploadUrl: session.upload_url,
        destinationPath: session.destination_path,
        size: prepared.blob.size,
        mimeType: prepared.mimeType,
      }));
    }

    item.destinationPath = session.destination_path || session.destinationPath || '';
    await uploadBlobInChunks(item, session.uploadUrl || session.upload_url, prepared.blob, prepared.mimeType, startByte, storageKey);
  } catch (error) {
    item.state = 'error';
    item.status = 'Failed';
    item.error = error.message || 'Upload failed';
    item.progress = 0;
    render();
  }
}

async function createUploadSession(prepared) {
  const response = await fetch('/api/create-upload-session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      region: regionInput.value,
      branch_id: branchInput.value,
      asset_type: assetInput.value,
      file_type: fileTypeInput.value,
      relative_path: prepared.relativePath,
      mime_type: prepared.mimeType,
      size: prepared.blob.size,
    }),
  });

  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || `Could not create upload session (${response.status})`);
  }
  return payload;
}

async function uploadBlobInChunks(item, uploadUrl, blob, mimeType, startByte, storageKey) {
  if (blob.size === 0) {
    item.status = 'Uploading';
    render();
    const response = await putEmptyUpload(uploadUrl, mimeType);
    if (response.status === 200 || response.status === 201) {
      localStorage.removeItem(storageKey);
      markDone(item, response.body || {}, 0);
      return;
    }
    throw new Error(response.bodyText || `Google Drive upload failed (${response.status})`);
  }

  let offset = startByte || 0;
  item.uploadedBytes = offset;
  item.progress = blob.size ? Math.floor((offset / blob.size) * 100) : 0;

  while (offset < blob.size) {
    const end = Math.min(offset + CHUNK_SIZE, blob.size) - 1;
    const chunk = blob.slice(offset, end + 1, mimeType);
    item.status = 'Uploading';
    render();

    const response = await putChunk(uploadUrl, chunk, offset, end, blob.size, mimeType, (loaded) => {
      item.uploadedBytes = offset + loaded;
      item.progress = blob.size ? Math.min(99, Math.floor((item.uploadedBytes / blob.size) * 100)) : 99;
      render();
    });

    if (response.status === 200 || response.status === 201) {
      localStorage.removeItem(storageKey);
      markDone(item, response.body || {}, blob.size);
      return;
    }

    if (response.status === 308) {
      offset = parseNextByte(response.rangeHeader, end + 1);
      item.uploadedBytes = offset;
      item.progress = blob.size ? Math.min(99, Math.floor((offset / blob.size) * 100)) : 99;
      render();
      continue;
    }

    if (response.status >= 500) {
      await delay(1200);
      const status = await queryUploadStatus(uploadUrl, blob.size);
      if (status.done) {
        localStorage.removeItem(storageKey);
        markDone(item, status.body || {}, blob.size);
        return;
      }
      offset = status.nextByte;
      continue;
    }

    throw new Error(response.bodyText || `Google Drive upload failed (${response.status})`);
  }
}

function putEmptyUpload(uploadUrl, mimeType) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', uploadUrl);
    request.setRequestHeader('Content-Type', mimeType || 'application/octet-stream');
    request.addEventListener('load', () => {
      resolve({
        status: request.status,
        body: parseJson(request.responseText),
        bodyText: request.responseText || '',
      });
    });
    request.addEventListener('error', () => reject(new Error('Network error while uploading to Google Drive')));
    request.send(new Blob([], { type: mimeType || 'application/octet-stream' }));
  });
}

function putChunk(uploadUrl, chunk, start, end, total, mimeType, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', uploadUrl);
    request.setRequestHeader('Content-Type', mimeType || 'application/octet-stream');
    request.setRequestHeader('Content-Range', `bytes ${start}-${end}/${total}`);

    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress(event.loaded);
    });

    request.addEventListener('load', () => {
      resolve({
        status: request.status,
        rangeHeader: request.getResponseHeader('Range') || '',
        body: parseJson(request.responseText),
        bodyText: request.responseText || '',
      });
    });
    request.addEventListener('error', () => reject(new Error('Network error while uploading to Google Drive')));
    request.addEventListener('timeout', () => reject(new Error('Google Drive upload timed out')));
    request.send(chunk);
  });
}

function queryUploadStatus(uploadUrl, total) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('PUT', uploadUrl);
    request.setRequestHeader('Content-Range', `bytes */${total}`);
    request.addEventListener('load', () => {
      if (request.status === 200 || request.status === 201) {
        resolve({ done: true, body: parseJson(request.responseText) });
        return;
      }
      if (request.status === 308) {
        resolve({
          done: false,
          nextByte: parseNextByte(request.getResponseHeader('Range') || '', 0),
        });
        return;
      }
      if (request.status === 404) {
        reject(new Error('Upload session expired'));
        return;
      }
      reject(new Error(request.responseText || `Could not query upload status (${request.status})`));
    });
    request.addEventListener('error', () => reject(new Error('Network error while checking upload status')));
    request.send();
  });
}

async function prepareFile(file, relativePath) {
  const mimeType = file.type || guessMimeType(relativePath);
  if (!mimeType.startsWith('image/') || file.size <= MAX_IMAGE_BYTES) {
    return { blob: file, relativePath, mimeType };
  }

  try {
    const compressed = await compressImage(file);
    if (!compressed || compressed.size >= file.size) {
      return { blob: file, relativePath, mimeType };
    }
    return {
      blob: compressed,
      relativePath: replaceExtension(relativePath, '.jpg'),
      mimeType: 'image/jpeg',
    };
  } catch (error) {
    return { blob: file, relativePath, mimeType };
  }
}

async function compressImage(file) {
  const bitmap = await createImageBitmap(file);
  let width = bitmap.width;
  let height = bitmap.height;
  let bestBlob = null;

  for (let scaleIndex = 0; scaleIndex < 12; scaleIndex += 1) {
    const scale = Math.pow(0.88, scaleIndex);
    width = Math.max(1, Math.round(bitmap.width * scale));
    height = Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: false });
    context.drawImage(bitmap, 0, 0, width, height);

    for (let quality = 0.9; quality >= 0.42; quality -= 0.08) {
      const blob = await canvasToBlob(canvas, 'image/jpeg', quality);
      if (!bestBlob || blob.size < bestBlob.size) bestBlob = blob;
      if (blob.size <= MAX_IMAGE_BYTES) {
        bitmap.close?.();
        return blob;
      }
    }
  }

  bitmap.close?.();
  return bestBlob;
}

function canvasToBlob(canvas, type, quality) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error('Could not compress image'));
    }, type, quality);
  });
}

async function getDroppedFiles(dataTransfer) {
  const entries = Array.from(dataTransfer.items || [])
    .map((item) => item.webkitGetAsEntry?.())
    .filter(Boolean);

  if (!entries.length) return Array.from(dataTransfer.files || []);

  const files = [];
  for (const entry of entries) {
    files.push(...await readEntry(entry, ''));
  }
  return files;
}

async function readEntry(entry, prefix) {
  if (entry.isFile) {
    const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
    file.relativePath = normalizeRelativePath(`${prefix}${file.name}`);
    return [file];
  }

  if (!entry.isDirectory) return [];

  const reader = entry.createReader();
  const children = [];
  let batch = [];
  do {
    batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    children.push(...batch);
  } while (batch.length > 0);

  const nested = [];
  for (const child of children) {
    nested.push(...await readEntry(child, `${prefix}${entry.name}/`));
  }
  return nested;
}

function markDone(item, body, uploadedSize) {
  item.state = 'done';
  item.status = 'Uploaded';
  item.progress = 100;
  item.uploadedBytes = uploadedSize;
  item.link = body.webViewLink || '';
  item.error = '';
  render();
}

function render() {
  fileCount.textContent = String(items.length);
  totalSize.textContent = formatBytes(items.reduce((sum, item) => sum + (item.uploadSize || item.file.size), 0));
  uploadBtn.disabled = isUploading || !items.some((item) => item.state === 'ready' || item.state === 'error') || Boolean(validateDestination());
  clearBtn.disabled = isUploading;

  const totalBytes = items.reduce((sum, item) => sum + (item.uploadSize || item.file.size), 0);
  const uploadedBytes = items.reduce((sum, item) => {
    if (item.state === 'done') return sum + (item.uploadSize || item.file.size);
    return sum + Math.min(item.uploadedBytes || 0, item.uploadSize || item.file.size);
  }, 0);
  const percent = totalBytes ? Math.floor((uploadedBytes / totalBytes) * 100) : 0;
  overallPercent.textContent = `${percent}%`;
  overallBar.style.width = `${percent}%`;

  if (!items.length) {
    fileList.innerHTML = '<div class="empty">No files selected.</div>';
    return;
  }

  fileList.innerHTML = items.map((item) => `
    <article class="file-row">
      <div class="file-name">
        <strong>${escapeHtml(item.uploadRelativePath || item.relativePath)}</strong>
        ${item.destinationPath ? `<span>${escapeHtml(item.destinationPath)}</span>` : `<span>${escapeHtml(item.file.type || guessMimeType(item.relativePath))}</span>`}
        ${item.link ? `<a href="${escapeAttribute(item.link)}" target="_blank" rel="noopener">Open in Google Drive</a>` : ''}
        ${item.error ? `<div class="file-error">${escapeHtml(item.error)}</div>` : ''}
      </div>
      <div>${formatBytes(item.uploadSize || item.file.size)}</div>
      <div class="file-status">
        <span class="badge ${item.state}">${escapeHtml(item.status)}</span>
        <div class="meter" aria-hidden="true"><span style="width:${item.progress}%"></span></div>
      </div>
    </article>
  `).join('');
}

async function checkHealth() {
  try {
    const response = await fetch('/api/health');
    const payload = await response.json();
    backendStatus.textContent = response.ok && payload.ok ? 'Backend: ready' : 'Backend: issue';
    backendStatus.className = response.ok && payload.ok ? 'status ok' : 'status error';
    driveStatus.textContent = payload.configured ? 'Drive: configured' : 'Drive: missing config';
    driveStatus.className = payload.configured ? 'status ok' : 'status warn';
  } catch (error) {
    backendStatus.textContent = 'Backend: offline';
    backendStatus.className = 'status error';
    driveStatus.textContent = 'Drive: unknown';
    driveStatus.className = 'status warn';
  }
}

function validateDestination() {
  if (!/^\d{7}$/.test(branchInput.value)) return 'กรุณากรอกรหัสสาขา 7 หลัก';
  return '';
}

function buildStorageKey(item, prepared) {
  const destination = [
    regionInput.value,
    branchInput.value,
    assetInput.value,
    fileTypeInput.value,
    prepared.relativePath,
    item.file.size,
    item.file.lastModified || 0,
  ].join('|');
  return `${STORAGE_PREFIX}${destination}`;
}

function readStoredSession(storageKey) {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) || 'null');
    return parsed && parsed.uploadUrl ? parsed : null;
  } catch (error) {
    return null;
  }
}

function parseNextByte(rangeHeader, fallback) {
  const match = /bytes=0-(\d+)/.exec(rangeHeader || '');
  return match ? Number(match[1]) + 1 : fallback;
}

function parseJson(text) {
  try {
    return JSON.parse(text || '{}');
  } catch (error) {
    return {};
  }
}

function normalizeRelativePath(path) {
  return String(path || 'file')
    .replaceAll('\\', '/')
    .replace(/^\/+/, '')
    .split('/')
    .filter((part) => part && part !== '.' && part !== '..')
    .join('/');
}

function replaceExtension(path, extension) {
  const normalized = normalizeRelativePath(path);
  const parts = normalized.split('/');
  const name = parts.pop() || 'file';
  const stem = name.includes('.') ? name.slice(0, name.lastIndexOf('.')) : name;
  parts.push(`${stem}${extension}`);
  return parts.join('/');
}

function guessMimeType(path) {
  const lower = String(path).toLowerCase();
  if (lower.endsWith('.jpg') || lower.endsWith('.jpeg')) return 'image/jpeg';
  if (lower.endsWith('.png')) return 'image/png';
  if (lower.endsWith('.webp')) return 'image/webp';
  if (lower.endsWith('.pdf')) return 'application/pdf';
  if (lower.endsWith('.csv')) return 'text/csv';
  if (lower.endsWith('.json')) return 'application/json';
  return 'application/octet-stream';
}

function showMessage(text, level) {
  if (!text) {
    message.hidden = true;
    message.textContent = '';
    message.className = 'message';
    return;
  }
  message.hidden = false;
  message.textContent = text;
  message.className = `message ${level || 'ok'}`;
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / Math.pow(1024, index);
  return `${value.toFixed(index === 0 ? 0 : 2)} ${units[index]}`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttribute(value) {
  return escapeHtml(value).replaceAll('`', '&#096;');
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
