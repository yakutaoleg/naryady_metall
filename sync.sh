#!/bin/bash
cd /root/naryady/test
venv/bin/python src/sync.py >> logs/sync.log 2>&1
