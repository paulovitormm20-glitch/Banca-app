@echo off
cd /d "%~dp0"
echo Instalando dependencias (so na primeira vez, pode demorar um pouco)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo.
echo Iniciando o Gestor de Banca...
echo O navegador vai abrir sozinho em alguns segundos.
echo NAO FECHE esta janela preta enquanto estiver usando o programa.
echo Para parar o programa, feche esta janela.
echo.
start "" cmd /c "timeout /t 3 /nobreak >nul && start "" http://localhost:8000"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
pause
