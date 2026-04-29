# @title PWA Image Management System (Sub-folder Support)
from google.colab import drive, files
import os
import ipywidgets as widgets
from IPython.display import display, clear_output
from PIL import Image
import shutil

# 1. เชื่อมต่อ Google Drive
print("กำลังเชื่อมต่อ Google Drive...")
drive.mount('/content/drive')

# 2. ตั้งค่าพื้นฐาน
BASE_DRIVE_PATH = '/content/drive/MyDrive/upload_pwa'

# 3. สร้าง UI Components
style = {'description_width': 'initial'}
reg_select = widgets.Dropdown(options=[f"reg{i}" for i in range(1, 11)], description='1. เลือกเขต (Reg):', style=style)
branch_id = widgets.Text(placeholder='เช่น 5531011', description='2. รหัสสาขา (7 หลัก):', style=style)
asset_select = widgets.Dropdown(options=["valve", "firehydrant", "pwa_waterworks"], description='3. ชั้นข้อมูล:', style=style)

# เพิ่มตัวเลือก Folder ย่อย
subfolder_select = widgets.Dropdown(
    options=[('รูปถ่าย (picture_path)', 'picture_path'), ('แบบวาด (drawing_path)', 'drawing_path')],
    description='4. ประเภทไฟล์:',
    style=style
)

run_button = widgets.Button(description='เลือกไฟล์และเริ่ม Process', button_style='success', icon='upload', layout={'width': 'max-content'})
output_area = widgets.Output()

def process_and_upload(b):
    with output_area:
        clear_output()
        
        if not branch_id.value:
            print("❌ กรุณากรอกรหัสสาขาก่อนครับ")
            return

        # เปิดหน้าต่างเลือกไฟล์จากเครื่อง
        print(f"📂 กำลังจัดการ {asset_select.value} -> {subfolder_select.value}")
        print("กรุณากดปุ่ม 'Choose Files' เพื่อเลือกไฟล์...")
        uploaded = files.upload()

        if not uploaded:
            print("⚠️ ไม่มีไฟล์ถูกเลือก")
            return

        # สร้าง Path ปลายทาง: upload_pwa/regX/pwa_XXXXXXX/asset/subfolder
        final_dir = os.path.join(
            BASE_DRIVE_PATH, 
            reg_select.value, 
            f"pwa_{branch_id.value}", 
            asset_select.value, 
            subfolder_select.value
        )
        
        if not os.path.exists(final_dir):
            os.makedirs(final_dir)
            print(f"📁 สร้าง Folder ใน Drive: {final_dir}")

        print(f"🚀 กำลังประมวลผล {len(uploaded)} ไฟล์...")
        
        count = 0
        for filename in uploaded.keys():
            input_file = filename
            output_file = os.path.join(final_dir, filename)
            
            try:
                # ตรวจสอบว่าเป็นไฟล์รูปภาพหรือไม่ (สำหรับทำ Resize)
                if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                    with Image.open(input_file) as img:
                        if img.mode in ("RGBA", "P"):
                            img = img.convert("RGB")
                        
                        quality = 90
                        img.save(output_file, "JPEG", optimize=True, quality=quality)
                        
                        # วนลูปลดขนาดจนกว่าจะ <= 2MB
                        while os.path.getsize(output_file) > 2 * 1024 * 1024 and quality > 20:
                            quality -= 10
                            img.save(output_file, "JPEG", optimize=True, quality=quality)
                else:
                    # ถ้าไม่ใช่รูปภาพ (เช่น PDF หรือไฟล์แบบ) ให้ Copy ไปตรงๆ
                    shutil.copy(input_file, output_file)
                
                size_mb = os.path.getsize(output_file) / (1024 * 1024)
                print(f"✅ เรียบร้อย: {filename} ({size_mb:.2f} MB)")
                
                os.remove(input_file)
                count += 1
            except Exception as e:
                print(f"❌ Error ไฟล์ {filename}: {e}")
        
        print(f"\n✨ ดำเนินการสำเร็จทั้งหมด {count} ไฟล์")
        print(f"📍 ตรวจสอบที่: {final_dir}")

run_button.on_click(process_and_upload)

# 4. แสดงหน้าจอ UI
print("-" * 50)
print("PWA Image & Drawing Management System")
display(reg_select, branch_id, asset_select, subfolder_select, run_button, output_area)