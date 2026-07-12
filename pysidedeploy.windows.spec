[app]

title = Haypile
project_dir = .
input_file = app_gui.py
exec_directory = build/windows-deploy
project_file =
icon = build/Haypile.ico

[python]

python_path = .build-venv/Scripts/python.exe
packages = Nuitka==4.0
android_packages = buildozer==1.5.0,cython==0.29.33

[qt]

qml_files =
excluded_qml_plugins =
modules = Core,Gui,Svg,Widgets
plugins = iconengines,imageformats,platforms,styles

[android]

wheel_pyside =
wheel_shiboken =
plugins =

[nuitka]

macos.permissions =
mode = standalone
extra_args = --quiet --assume-yes-for-downloads --noinclude-qt-translations --include-data-dir=ui_assets=ui_assets --include-data-dir=assets=assets --output-filename=Haypile.exe --windows-console-mode=hide --company-name=Haypile --product-name=Haypile --file-description=Haypile --file-version=0.2.0.0 --product-version=0.2.0.0

[buildozer]

mode = debug
recipe_dir =
jars_dir =
ndk_path =
sdk_path =
local_libs =
arch =
