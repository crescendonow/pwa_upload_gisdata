# System Design: High-Concurrency Google Drive Folder Uploader

เอกสารสรุปแนวทางการพัฒนาระบบอัปโหลดไฟล์/โฟลเดอร์ขนาดใหญ่ (100GB+) เข้าสู่ Google Drive โดยรองรับ Concurrent User มากกว่า 100 ราย ภายใต้ทรัพยากรจำกัด (Railway Hobby Plan)

## 1. System Architecture: Direct Resumable Upload
เพื่อลดภาระของ Backend และ Bandwidth ของ Hosting เราจะใช้เทคนิค **Direct Upload** โดยให้ข้อมูลวิ่งตรงจาก Browser ของ User ไปยัง Google Drive API โดยไม่ผ่าน Server ของเรา

### Workflow:
1. **Frontend**: ผู้ใช้เลือก Folder -> ระบบทำการ List ไฟล์ทั้งหมด
2. **Handshake**: Frontend ส่ง Metadata (ชื่อไฟล์, ขนาด) ไปยัง FastAPI
3. **Session Creation**: FastAPI (Backend) ใช้ Google API เพื่อสร้าง `Resumable Upload URL`
4. **Direct Stream**: Frontend ได้รับ URL แล้วทำการหั่นไฟล์เป็นชิ้นๆ (Chunking) และส่ง (PUT Request) ไปยัง Google Drive โดยตรง
5. **Completion**: เมื่ออัปโหลดครบทุก Chunk ระบบจะแจ้งเตือนความสำเร็จ

---

## 2. Frontend Implementation (HTML/JS)
การจัดการโฟลเดอร์และไฟล์ขนาดใหญ่ต้องทำบนฝั่ง Client เป็นหลัก

- **Folder Selection**: ใช้ `<input type="file" webkitdirectory />`
- **File Chunking**: ใช้ `Blob.slice()` เพื่อแบ่งไฟล์ขนาดใหญ่เป็นส่วนๆ (เช่น ส่วนละ 10MB) เพื่อป้องกัน Browser ค้างและรองรับการ Resume เมื่อเน็ตหลุด
- **Concurrency Control (Client-Side)**: 
    - แม้จะมีผู้ใช้ 100 คน แต่ละคนควรมี **Worker Queue** ภายใน Browser ของตัวเอง
    - จำกัดการอัปโหลดพร้อมกันที่ 3-5 ไฟล์ต่อคน เพื่อรักษาความเสถียรของ Network Interface
- **State Management**: เก็บสถานะการอัปโหลด (Upload URL, Chunk Index) ไว้ใน `localStorage` เพื่อให้สามารถอัปโหลดต่อได้หาก User รีเฟรชหน้าจอ

---

## 3. Backend Implementation (FastAPI)
ทำหน้าที่เป็น **Orchestrator** เท่านั้น ไม่ทำหน้าที่เป็น Data Proxy เพื่อให้การรันบน Environment ที่มีข้อจำกัดด้าน RAM อย่าง Railway ทำงานได้อย่างมีประสิทธิภาพ

- **Auth**: จัดการ OAuth2 / Service Account เพื่อออก Access Token
- **API Endpoints**:
    - `POST /create-upload-session`: รับรายละเอียดไฟล์ -> คุยกับ Google Drive API -> คืนค่า `upload_url`
- **Async Implementation**: ใช้ `async def` คู่กับไลบรารีอย่าง HTTPX เพื่อรองรับ Concurrent Request จำนวนมากโดยไม่บล็อก Thread
- **Rate Limiting**: ติดตั้ง `slowapi` หรือใช้ Redis บน Railway เพื่อคุมไม่ให้ Request ไปยัง Google API เกินโควต้า (ป้องกัน Error 429)

---

## 4. กลยุทธ์การจัดการ Concurrency > 100

| ส่วนประกอบ | วิธีจัดการ |
| :--- | :--- |
| **Network Bandwidth** | ใช้ Direct Upload ไปยัง Google (Google รับโหลดได้มหาศาล) |
| **RAM Usage** | Backend ไม่ต้องเก็บไฟล์ใน Memory ( Stateless ) ทำให้ Railway Hobby Plan รับโหลดได้สบาย |
| **API Quota** | ใช้ Exponential Backoff ในการขอ Session URL และใช้ Queue ฝั่ง Frontend |
| **Database** | หากต้องเก็บประวัติการอัปโหลด แนะนำ MongoDB Atlas หรือ PostgreSQL ที่รันคู่กันบน Railway |

---

## 5. การตั้งค่า Hosting (Railway.app)
- **Service**: รัน FastAPI บน Docker Container
- **Environment Variables**: เก็บ `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `REDIRECT_URI` ให้ปลอดภัย
- **Optimization**: เนื่องจากเป็นงาน I/O Bound (รอ Network) แนะนำให้ปรับ Gunicorn/Uvicorn workers ให้เหมาะสมกับจำนวน CPU Core

---

## 6. สรุปคำแนะนำเพิ่มเติม
1. **Security**: ตรวจสอบสิทธิ์ (Permissions) ใน Google Drive ให้ดีก่อนสร้าง Session URL เพื่อป้องกันคนนอกแอบใช้งาน
2. **UX**: เพิ่ม Progress Bar รายไฟล์ และ Progress Bar รวมของทั้ง Folder
3. **Resiliency**: ในกรณีไฟล์ 100GB หากเน็ตตัด ระบบ Resumable Upload จะเป็นตัวช่วยสำคัญที่ทำให้ไม่ต้องเริ่มใหม่ตั้งแต่ 0
