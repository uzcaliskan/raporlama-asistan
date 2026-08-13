"""Modelin cagirabilecegi araclari ve fonksiyonlari icerir."""

import os
import re
import json
import smtplib
import subprocess
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

import pandas as pd
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import requests
import html

import ollama_client

TIMEOUT = 20
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Türkçe karakter destekli font ayarı
_OLASI_FONT_YOLLARI = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]

def _font_kaydet():
    normal_yol, bold_yol = None, None
    for yol in _OLASI_FONT_YOLLARI:
        if os.path.exists(yol):
            if "bold" in yol.lower() or "bd" in yol.lower():
                bold_yol = bold_yol or yol
            else:
                normal_yol = normal_yol or yol
    if normal_yol is None:
        normal_yol = _OLASI_FONT_YOLLARI[0]
    pdfmetrics.registerFont(TTFont("TRFont", normal_yol))
    pdfmetrics.registerFont(TTFont("TRFont-Bold", bold_yol or normal_yol))
    return "TRFont", "TRFont-Bold"

FONT_NORMAL, FONT_BOLD = _font_kaydet()

CLASSIFICATION_SYSTEM_PROMPT = """Cevabını SADECE şu JSON formatında ver, başka hiçbir açıklama ekleme:
{"durum": "Yaklaşan Operasyon" | "Öncelikli" | "Hiçbiri", "aciklama": "<1 cümlelik, güncel operasyonu özetleyen Türkçe açıklama>"}

Sen sondaj günlük raporlarını analiz eden bir uzmansın. Sana tek bir kuyunun güncel raporu verilecek. Görevin bu kuyuyu İKİ kategoriden birine sınıflandırmak, ya da hiçbirine uymuyorsa "Hiçbiri" demek.

KATEGORİ 1 - "Yaklaşan Operasyon":
Kuyudaki son casing çapı 13 3/8" ise, YA DA güncel aktif sondaj/genişletme fazı 12 1/4" ya da daha küçük bir çapsa (12 1/4, 10 5/8, 9 5/8, 8 1/2, 7, 6x7, 5 1/2, 5, 4 1/2), bu kategoriye girer.
Not: "(Program)" ile başlayan sütunlar NİHAİ HEDEF değerleridir, güncel durumu yansıtmaz - bunları kullanma. Güncel fazı İş Programı ve 08:00 Durumu serbest metninden çıkar.

KATEGORİ 2 - "Öncelikli":
Raporda "fullset", "full set", "log alımı", "log operasyonu" gibi ifadeler geçiyorsa (şu an alınıyor OLSUN, ya da İş Programı'nda alınacağı belirtiliyor OLSUN), bu kategoriye girer.

Bir kuyu HEM Kategori 1 HEM Kategori 2'ye uyabilir - bu durumda "Öncelikli" seç (daha yüksek öncelik).
Kuyu hiçbir kategoriye uymuyorsa (örn. sadece nakliyat/montaj/demontaj faaliyeti, ya da düz rutin sondaj devam ediyor ve yukarıdaki koşullar geçerli değilse), "Hiçbiri" de.

"aciklama" alanı, PDF raporunda gösterilecek kısa bir özet olacak (örnek: "12 1/4 sondaj yapılıyor", "9 5/8 çimentolandı, 8 1/2 section'a geçiliyor", "Fullset alınıyor, 7'lik casing inişi")."""


def _satiri_metne_cevir(row):
    satirlar = []
    for kolon in row.index:
        deger = row[kolon]
        if deger is None or (isinstance(deger, float) and deger != deger):
            deger = "-"
        satirlar.append(f"{kolon}: {deger}")
    return "\n".join(satirlar)


def internet_search(query: str, max_results: int = 5) -> str:
    """DuckDuckGo üzerinden internet araması yapar."""
    try:
        response = requests.post(
            "https://lite.duckduckgo.com/lite/",
            data={"q": query},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        pairs = re.findall(
            r"""<a[^>]*href="([^"]+)"[^>]*class=['"]result-link['"][^>]*>(.*?)</a>""",
            response.text,
            flags=re.DOTALL,
        )
        results = []
        for url, raw_title in pairs[:max_results]:
            title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
            if title:
                results.append(f"{len(results) + 1}. {title}\n   {html.unescape(url)}")
        if results:
            return f"'{query}' için internet sonuçları:\n" + "\n".join(results)
    except requests.RequestException:
        pass
    return f"'{query}' için sonuç bulunamadı."


def process_excel_and_generate_report(excel_path: str, output_pdf_path: str = "Gunluk_Durum_Raporu.pdf") -> str:
    """Excel dosyasını okur, her satırı modelle sınıflandırır ve PDF raporu üretir."""
    if not os.path.exists(excel_path):
        return f"Hata: '{excel_path}' dosyası bulunamadı."

    try:
        df = pd.read_excel(excel_path)
    except Exception as exc:
        return f"Excel dosyası okunurken hata oluştu: {exc}"

    raporlanacak_kuyular = []

    for index, row in df.iterrows():
        rapor_metni = _satiri_metne_cevir(row)
        messages = [
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Aşağıdaki kuyu raporunu analiz et:\n\n{rapor_metni}"},
        ]

        try:
            response = ollama_client.chat(messages, temperature=0.2)
            ham_cevap = response.get("content", "")
        except Exception:
            continue

        json_eslesme = re.search(r"\{.*\}", ham_cevap, re.DOTALL)
        if not json_eslesme:
            continue

        try:
            sonuc = json.loads(json_eslesme.group(0))
        except json.JSONDecodeError:
            continue

        durum = sonuc.get("durum", "Hiçbiri")
        if durum in ("Yaklaşan Operasyon", "Öncelikli"):
            raporlanacak_kuyular.append({
                "kuyu_adi": row.get("Kuyu Adı", f"Kuyu-{index+1}"),
                "durum": durum,
                "aciklama": sonuc.get("aciklama", "").strip(),
                "tur": row.get("Kuyu Türü", "Arama")
            })

    styles = getSampleStyleSheet()
    baslik_stili = ParagraphStyle("TRBaslik", parent=styles["Title"], fontName=FONT_BOLD)
    normal_stili = ParagraphStyle("TRNormal", parent=styles["Normal"], fontName=FONT_NORMAL)
    hucre_stili = ParagraphStyle("Hucre", parent=styles["Normal"], fontName=FONT_NORMAL, fontSize=8, leading=10)

    doc = SimpleDocTemplate(
        output_pdf_path, pagesize=landscape(A4),
        leftMargin=1.5 * cm, rightMargin=1.5 * cm, topMargin=1.5 * cm, bottomMargin=1.5 * cm,
    )
    story = []

    story.append(Paragraph("Günlük Durum Raporu Özetleri", baslik_stili))
    story.append(Paragraph(f"Tarih: {datetime.now().strftime('%d.%m.%Y')}", normal_stili))
    story.append(Spacer(1, 12))

    if not raporlanacak_kuyular:
        story.append(Paragraph("Bugün için kriterlere uyan kuyu bulunamadı.", normal_stili))
    else:
        veri = [["Kuyu", "Durum", "Açıklama", "Tür"]]
        for k in raporlanacak_kuyular:
            veri.append([
                Paragraph(str(k.get("kuyu_adi", "-")), hucre_stili),
                Paragraph(str(k.get("durum", "-")), hucre_stili),
                Paragraph(str(k.get("aciklama", "-")), hucre_stili),
                Paragraph(str(k.get("tur", "-")), hucre_stili),
            ])

        tablo = Table(veri, colWidths=[4 * cm, 3.5 * cm, 12 * cm, 3 * cm], repeatRows=1)
        tablo.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), FONT_BOLD),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("ALIGN", (0, 0), (-1, 0), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F2F2")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(tablo)

    doc.build(story)
    return f"PDF raporu başarıyla oluşturuldu: {output_pdf_path}. Toplam {len(raporlanacak_kuyular)} kuyu rapora eklendi."


def execute_python_code(code: str) -> str:
    """Modelin ürettiği Python kodunu güvenli bir subprocess ortamında çalıştırır ve çıktısını döner."""
    temp_script = "temp_execution_script.py"
    try:
        with open(temp_script, "w", encoding="utf-8") as f:
            f.write(code)
        
        result = subprocess.run(
            ["python", temp_script],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = ""
        if result.stdout:
            output += f"Çıktı (Stdout):\n{result.stdout}\n"
        if result.stderr:
            output += f"Hata (Stderr):\n{result.stderr}\n"
        if not output:
            output = "Kod başarıyla çalıştırıldı (Çıktı üretmedi)."
            
        return output
    except Exception as exc:
        return f"Python kodu çalıştırılırken hata oluştu: {exc}"
    finally:
        if os.path.exists(temp_script):
            os.remove(temp_script)


def send_gmail(recipients: list[str], subject: str, body: str, pdf_path: str) -> str:
    """Oluşturulan PDF raporunu gerçek Gmail hesabı üzerinden e-posta olarak gönderir."""
    sender_email = os.getenv("GMAIL_USER", "uzcaliskan")
    password = os.getenv("GMAIL_APP_PASSWORD", "")

    try:
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender_email
        msg['To'] = ", ".join(recipients)
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=os.path.basename(pdf_path))
                part['Content-Disposition'] = f'attachment; filename="{os.path.basename(pdf_path)}"'
                msg.attach(part)

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, password)
            server.sendmail(sender_email, recipients, msg.as_string())
            
        return f"E-posta başarıyla gönderildi. Alıcılar: {', '.join(recipients)}"
    except Exception as exc:
        return f"E-posta gönderilirken hata oluştu: {exc}"


TOOLS = {
    "internet_search": internet_search,
    "process_excel_and_generate_report": process_excel_and_generate_report,
    "execute_python_code": execute_python_code,
    "send_gmail": send_gmail,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "internet_search",
            "description": "Güncel olaylar veya genel bilgi için internette arama yapar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Arama sorgusu"},
                    "max_results": {"type": "integer", "description": "Sonuç sayısı (varsayılan 5)"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_excel_and_generate_report",
            "description": "Excel dosyasını satır satır okur, LLM ile sınıflandırıp PDF formatında günlük durum raporu üretir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "excel_path": {"type": "string", "description": "Excel dosyasının dosya yolu"},
                    "output_pdf_path": {"type": "string", "description": "Çıktı PDF dosyasının adı"}
                },
                "required": ["excel_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python_code",
            "description": "Python kodunu subprocess ile yerel sistemde çalıştırır ve sonucunu döner.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Çalıştırılacak Python kod blokları"}
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_gmail",
            "description": "Hazırlanan PDF raporunu belirtilen e-posta adreslerine Gmail üzerinden gönderir.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alıcı e-posta adresleri listesi"
                    },
                    "subject": {"type": "string", "description": "E-posta konusu"},
                    "body": {"type": "string", "description": "E-posta metin içeriği"},
                    "pdf_path": {"type": "string", "description": "Gönderilecek PDF dosyasının yolu"}
                },
                "required": ["recipients", "subject", "body", "pdf_path"],
            },
        },
    },
]