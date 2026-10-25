from pathlib import Path

path = Path("/opt/signalcopyreal/gateway/order_intent.py")
content = path.read_text(encoding="utf-8")
if "AUTONOMOUS_SCANNER" not in content:
    content = content.replace(
        'VALID_SOURCES = ("M30H1_ENGINE", "SIGNAL_COPY", "REVO_ADAPTIVE", "MANUAL", "TEST")',
        'VALID_SOURCES = ("M30H1_ENGINE", "SIGNAL_COPY", "REVO_ADAPTIVE", "MANUAL", "TEST", "AUTONOMOUS_SCANNER")'
    )
    path.write_text(content, encoding="utf-8")
    print("Added AUTONOMOUS_SCANNER to VALID_SOURCES")
else:
    print("AUTONOMOUS_SCANNER already present")
