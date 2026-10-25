from pathlib import Path

path = Path("/opt/signalcopyreal/gateway/service.py")
content = path.read_text(encoding="utf-8")
if 'skip_cluster_limit=intent.source in ("SIGNAL_COPY", "AUTONOMOUS_SCANNER")' not in content:
    content = content.replace(
        'skip_cluster_limit=intent.source == "SIGNAL_COPY",',
        'skip_cluster_limit=intent.source in ("SIGNAL_COPY", "AUTONOMOUS_SCANNER"),'
    )
    path.write_text(content, encoding="utf-8")
    print("Updated service.py for AUTONOMOUS_SCANNER")
else:
    print("Already updated in service.py")
