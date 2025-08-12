import tkinter as tk
from tkinter import filedialog, messagebox
import subprocess
import os
from pathlib import Path
import re
import tempfile
import shutil
import json
import sys

# --------- اعدادات ---------
# محاولة إيجاد ffmpeg/ffprobe محلياً (ضمن مجلد التطبيق أو مجلد PyInstaller المؤقت) ثم السقوط إلى PATH
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def resolve_tool(executable_name: str) -> str:
    candidates = [
        BASE_DIR / f"{executable_name}.exe",  # ويندوز
        BASE_DIR / executable_name,            # أنظمة أخرى
        executable_name,                       # من PATH
    ]
    for candidate in candidates:
        # إذا كان مساراً موجوداً أو يمكن العثور عليه عبر PATH
        if Path(candidate).exists() or shutil.which(str(candidate)) is not None:
            return str(candidate)
    return executable_name


FFMPEG = resolve_tool("ffmpeg")
FFPROBE = resolve_tool("ffprobe")

# ترتيب رقمي للأسماء
def natural_key(s: str):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'([0-9]+)', s)]

# فحص ffmpeg/ffprobe
def check_ffmpeg() -> bool:
    def tool_exists(tool_path: str) -> bool:
        return Path(tool_path).exists() or shutil.which(tool_path) is not None

    for tool in [FFMPEG, FFPROBE]:
        if not tool_exists(tool):
            base = Path(tool).name
            messagebox.showerror("خطأ", f"{base} غير موجود. ضع ffmpeg/ffprobe بجانب البرنامج أو أضِفهما إلى PATH")
            return False
    return True

# الحصول على معلومات الفيديو والصوت بصيغة JSON
def probe_streams(file_path: Path):
    cmd = [
        FFPROBE,
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_name,codec_type,profile,pix_fmt,width,height,field_order,avg_frame_rate,time_base,channels,channel_layout,sample_rate",
        "-of",
        "json",
        str(file_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل ffprobe على الملف: {file_path}\n{result.stderr}")
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"فشل تحليل ناتج ffprobe: {e}")

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    video_cmp = None
    if video is not None:
        video_cmp = {
            k: video.get(k)
            for k in [
                "codec_name",
                "profile",
                "pix_fmt",
                "width",
                "height",
                "field_order",
                "avg_frame_rate",
                "time_base",
            ]
        }

    audio_cmp = None
    if audio is not None:
        audio_cmp = {
            k: audio.get(k)
            for k in ["codec_name", "channels", "channel_layout", "sample_rate", "time_base"]
        }

    return {"video": video_cmp, "audio": audio_cmp}

# التأكد إذا الملفات متطابقة في تشكيلات الفيديو والصوت
def all_same_format(files: list[Path]) -> bool:
    try:
        first = probe_streams(files[0])
    except Exception as e:
        messagebox.showerror("خطأ", str(e))
        return False

    for f in files[1:]:
        try:
            info = probe_streams(f)
        except Exception as e:
            messagebox.showerror("خطأ", str(e))
            return False
        if info != first:
            return False
    return True

# هروب أسماء الملفات لقائمة concat الخاصة بـ ffmpeg
def ffconcat_escape(path: Path) -> str:
    # نستخدم علامات اقتباس مفردة ونستبدل ' داخل الاسم بالتسلسل '\''
    return path.as_posix().replace("'", "'\\''")

# دمج بدون إعادة ترميز
def concat_copy(files: list[Path], output: Path):
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8") as f:
        for file in files:
            f.write(f"file '{ffconcat_escape(file)}'\n")
        listfile = f.name
    try:
        cmd = [
            FFMPEG,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listfile,
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "فشل دمج بدون ترميز")
    finally:
        try:
            os.unlink(listfile)
        except OSError:
            pass

# فحص توفر NVENC
def has_nvenc() -> bool:
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return r.returncode == 0 and ("h264_nvenc" in r.stdout)
    except Exception:
        return False

# إعادة ترميز (GPU إن توفر وإلا CPU)
def reencode(files: list[Path], output: Path):
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt", encoding="utf-8") as f:
        for file in files:
            f.write(f"file '{ffconcat_escape(file)}'\n")
        listfile = f.name

    try:
        video_args = []
        if has_nvenc():
            # إعدادات NVENC حديثة (قد تختلف حسب الإصدار)
            video_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr_hq", "-cq", "19"]
        else:
            video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19"]

        cmd = [
            FFMPEG,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            listfile,
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "فشل إعادة الترميز")
    finally:
        try:
            os.unlink(listfile)
        except OSError:
            pass

# الوظيفة الرئيسية
def merge_videos():
    if not files:
        messagebox.showwarning("تنبيه", "اختر ملفات الفيديو أولاً")
        return

    out_file = filedialog.asksaveasfilename(defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
    if not out_file:
        return

    sorted_files = sorted(files, key=lambda x: natural_key(x.name))

    try:
        if all_same_format(sorted_files):
            concat_copy(sorted_files, Path(out_file))
            messagebox.showinfo("تم", "تم الدمج بسرعة بدون إعادة ترميز")
        else:
            reencode(sorted_files, Path(out_file))
            acc = "GPU" if has_nvenc() else "CPU"
            messagebox.showinfo("تم", f"تم الدمج مع إعادة ترميز ({acc})")
    except Exception as e:
        messagebox.showerror("خطأ", str(e))

# اختيار الملفات
def choose_files():
    chosen = filedialog.askopenfilenames(filetypes=[
        ("Video files", "*.mp4 *.mov *.mkv *.avi *.ts *.webm *.m4v"),
        ("All files", "*.*"),
    ])
    if chosen:
        files.clear()
        for c in chosen:
            files.append(Path(c))
        listbox.delete(0, tk.END)
        for f in sorted(files, key=lambda x: natural_key(x.name)):
            listbox.insert(tk.END, f.name)

# ------------------ GUI ------------------

def main():
    if not check_ffmpeg():
        return

    global files, listbox
    files = []

    root = tk.Tk()
    root.title("دمج الفيديوهات")
    root.geometry("520x420")
    root.resizable(False, False)

    lbl = tk.Label(root, text="اختر ملفات الفيديو للدمج", font=("Arial", 14))
    lbl.pack(pady=10)

    btn_choose = tk.Button(root, text="اختيار الملفات", command=choose_files)
    btn_choose.pack(pady=5)

    listbox = tk.Listbox(root, width=65, height=12)
    listbox.pack(pady=5)

    btn_merge = tk.Button(root, text="دمج وحفظ", command=merge_videos, bg="green", fg="white", font=("Arial", 12))
    btn_merge.pack(pady=20)

    root.mainloop()


if __name__ == "__main__":
    main()