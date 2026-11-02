#!/bin/sh
set -eu
PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import flask,fitz,cv2,PIL,numpy' 2>/dev/null; then
  .venv/bin/python -m pip install -r requirements.txt
fi
if [ "$(uname -s)" = Darwin ] && command -v swiftc >/dev/null 2>&1; then
  if [ ! -x tools/vision_ocr ] || [ tools/vision_ocr.swift -nt tools/vision_ocr ]; then
    echo '正在编译本地 Apple Vision OCR（不上传文件）…'
    swiftc tools/vision_ocr.swift -o tools/vision_ocr || echo 'OCR 编译未成功，仍可手动框选建库。'
  fi
fi
exec .venv/bin/python app.py
