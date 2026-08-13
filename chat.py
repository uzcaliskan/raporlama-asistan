"""Arac cagirabilen sohbet asistani (komut satiri)."""

import argparse
import ollama_client
import tools

MAX_TOOL_ROUNDS = 5

SYSTEM_PROMPT = """Sen Türkçe konuşan profesyonel bir operasyon ve yapay zeka asistanısın. 
Elinde şu araçlar var:
- internet_search: Güncel bilgileri aramak için.
- process_excel_and_generate_report: Sondaj Excel dosyasını analiz edip PDF raporu üretmek için.
- execute_python_code: Python kodlarını yazıp test etmek veya çalıştırmak için.
- send_gmail: Raporları alıcılara e-posta ile göndermek için.

Kullanıcının taleplerine uygun araçları seçerek sırasıyla ve hatasız bir şekilde çalıştır."""

parser = argparse.ArgumentParser(description="Ollama tabanlı akıllı asistan.")
parser.add_argument("--chat-model", default=ollama_client.CHAT_MODEL, help="Ollama sohbet modeli")
args = parser.parse_args()

print("Akıllı Asistan Devrede")
print(f"  sohbet modeli: {args.chat_model}")
print("  çıkmak için: cik\n")

messages = [{"role": "system", "content": SYSTEM_PROMPT}]

while True:
    try:
        question = input("Siz > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        break
    if not question:
        continue
    if question.lower() in {"cik", "çık", "exit", "quit"}:
        break

    messages.append({"role": "user", "content": question})

    try:
        for _ in range(MAX_TOOL_ROUNDS):
            message = ollama_client.chat(
                messages, model=args.chat_model, tools=tools.TOOL_SCHEMAS
            )
            messages.append(message)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                break
            
            for call in tool_calls:
                name = call["function"]["name"]
                arguments = call["function"].get("arguments") or {}
                
                # Modelin ürettiği Python kodunu terminalde görmek için özel blok
                if name == "execute_python_code":
                    print(f"\n  💻 Model Tarafından Üretilen Python Kodu:\n----------------------------------------\n{arguments.get('code')}\n----------------------------------------")
                else:
                    print(f"  🔧 Araç Çalıştırılıyor: {name}({arguments})")

                function = tools.TOOLS.get(name)
                if function is None:
                    output = f"'{name}' adında bir araç yok."
                else:
                    try:
                        output = function(**arguments)
                    except Exception as exc:
                        output = f"Araç çalıştırılamadı: {exc}"

                messages.append({"role": "tool", "tool_name": name, "content": str(output)})
    except RuntimeError as exc:
        print(f"\nHata: {exc}\n")
        continue

    print(f"\nAsistan > {(message.get('content') or '').strip()}\n")