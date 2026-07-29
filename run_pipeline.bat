@echo off
cd /d D:\Youtube_Agent\youtube-automation

echo ===== Starting YouTube Automation Pipeline =====

python agents/topic_agent.py
if errorlevel 1 goto :error

python agents/research_agent.py
if errorlevel 1 goto :error

python agents/script_agent.py
if errorlevel 1 goto :error

python agents/voice_agent.py
if errorlevel 1 goto :error

python agents/media_agent.py
if errorlevel 1 goto :error

python agents/editor_agent.py
if errorlevel 1 goto :error

python agents/thumbnail_agent.py
if errorlevel 1 goto :error

python agents/seo_agent.py
if errorlevel 1 goto :error

python agents/upload_agent.py
if errorlevel 1 goto :error

echo ===== Pipeline Complete! Video Uploaded =====
exit /b 0

:error
echo ===== Pipeline FAILED at some step =====
exit /b 1