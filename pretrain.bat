if exist ./data/wikitext-103-raw-v1.txt (
    echo "wikitext-103-raw-v1.txt exists"
) else (
    python data.py
)
python -W ignore pretrain.py ^
    --train_cfg config/pretrain.json ^
    --model_cfg config/bert_base.json ^
    --data_file data/wikitext-103-raw-v1.txt ^
    --vocab data/vocab.txt ^
    --save_dir model ^
    --max_len 512 ^
    --max_pred 20 ^
    --mask_prob 0.15