python qan.py --save-dir log/test \
    --evaluate --resume log/qan-res50-ilidsvid/best_model.pth.tar \
	-test-batch 4 --gpu-devices 0 --pool qan
