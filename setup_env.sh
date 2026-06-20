#!/bin/sh

# Locate this script.
# When run directly: $0 is the script path.
# When sourced:      $_ holds the path used to source (ash and bash both set this).
if [ "$(basename -- "$0")" = "setup_env.sh" ]; then
    _direct=1
    _self="$0"
else
    _direct=0
    _self="${_:-./setup_env.sh}"
fi

_dir=$(cd "$(dirname -- "$_self")" 2>/dev/null && pwd)
: "${_dir:=$(pwd)}"
venvdir=$_dir/.venv

if [ ! -d "$venvdir" ] || [ "$_direct" = "1" ]; then
    echo "setup virtual env $venvdir"
    python3 -m venv "$venvdir"
    . "$venvdir/bin/activate"
    pip3 install --upgrade --editable "$_dir[dev]"
fi

if [ "$_direct" = "1" ]; then
    echo "now activate your virtual env with \". $_self\""
else
    echo "activate virtual env"
    . "$venvdir/bin/activate"
    echo "type \"deactivate\" to exit the virtual env"
fi

ROOTDIR=$_dir

case ":$PATH:" in
    *":$ROOTDIR/tools:"*) ;;
    *) PATH="$ROOTDIR/tools:$PATH" ;;
esac

export PATH
export ROOTDIR
