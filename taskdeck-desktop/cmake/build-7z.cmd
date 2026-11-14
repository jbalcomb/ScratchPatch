@echo off
rem Builds 7z.dll (7-Zip's archive library, every format incl. RAR and encryption) with 7-Zip's own makefile,
rem as 7-Zip's releases are built. Called by CMakeLists.txt:
rem   build-7z.cmd <vcvars64.bat> <7-Zip source> <output folder>
rem 7-Zip writes its objects inside the source tree (CPP\7zip\Bundles\Format7zF\x64), here a copy CMake fetched.
call "%~1" >nul 2>&1
if errorlevel 1 exit /b 1
cd /d "%~2\CPP\7zip\Bundles\Format7zF" || exit /b 1
rem MY_STATIC_LINK: the C runtime linked in, like TaskDeck.exe (no Visual C++ redistributable needed).
nmake /nologo NEW_COMPILER=1 MY_STATIC_LINK=1 >"%~3\build-7z.log" 2>&1
if errorlevel 1 (
  type "%~3\build-7z.log"
  exit /b 1
)
copy /y x64\7z.dll "%~3\7z.dll" >nul
