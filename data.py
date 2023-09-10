import datasets
import re

#wikitext-103
wiki = datasets.load_dataset('wikitext', 'wikitext-103-raw-v1', split='train')
file = open("data/wikitext-103-raw-v1.txt", "w", encoding="utf-8")

for i in wiki["text"]:
    if len(re.findall(" = ", i))>1:
        i = "\n"
    file.write(i)