#!/bin/bash

py-spy record -f=speedscope -r 10  -o /app/report/report.svg -- python3 app.py
