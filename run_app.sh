#!/usr/bin/env bash

echo " "
echo "==================================================================================================="
echo "==================================================================================================="

source activate test
conda info

echo " "
echo "==================================================================================================="
echo "==================================================================================================="

uvicorn --host=0.0.0.0 --port=${PORT:-5000} repodata_tools.app:app
