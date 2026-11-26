"""Xuất mô tả API ra file openapi.json (chuẩn OpenAPI 3) - nộp kèm báo cáo hoặc nạp vào Swagger Editor / Postman.

    python export_openapi.py                 # -> openapi.json (cạnh file này)
    python export_openapi.py out.json http://192.168.1.10:8000
"""
import json
import sys
from pathlib import Path

import config
from api_server import create_app


def export(path=None, server_url="http://localhost:%d" % config.API_PORT) -> Path:
    path = Path(path or config.BASE_DIR / "openapi.json")
    spec = create_app(db_path=":memory:").openapi()
    spec["servers"] = [{"url": server_url, "description": "Máy bảo vệ (đổi IP theo mạng LAN)"}]
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    p = export(*(sys.argv[1:3]))
    print("Đã xuất", p)
