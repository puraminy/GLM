EXPERIMENT_NAME=${MODEL_TYPE}-copa
TASK_NAME=COPA
DATA_PATH="${DATA_ROOT}/COPA"
MAX_SEQ_LEN=256

LR_SINGLE=1e-5
EPOCH_SINGLE=20

TRAIN_ARGS="--lr-decay-style linear \
            --warmup 0.1 \
            --weight-decay 1.0e-1"

COMMON_ARGS="--save-interval 10000 \
             --log-interval 20 \
             --eval-interval 1000 \
             --eval-iters 100"