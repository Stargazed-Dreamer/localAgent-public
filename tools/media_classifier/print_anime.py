import json

jsonl = r'<project_root>\output\<data_drive>:\<bilibili_organized_output>\classification_results.jsonl'
items = []
with open(jsonl, encoding='utf-8') as f:
    for line in f:
        items.append(json.loads(line))

anime = [i for i in items if i.get('category_name') == '动漫二次元']
print(f'【动漫二次元】共 {len(anime)} 条:')
print('=' * 100)
for n, i in enumerate(anime, 1):
    conf = i.get('confidence', 0)
    mark = '✓' if conf >= 0.8 else ('~' if conf >= 0.5 else '?')
    fn = i['filename'][:70]
    reason = i.get('reason', '')[:35]
    danmaku = ' [弹幕]' if i.get('has_danmaku') else ''
    print(f'  {n:3d}. [{mark}] {fn}{danmaku}')
    print(f'       reason: {reason}')
print('=' * 100)
print(f'高置信(≥0.8): {sum(1 for i in anime if i.get("confidence",0)>=0.8)}')
print(f'中置信(0.5-0.8): {sum(1 for i in anime if 0.5<=i.get("confidence",0)<0.8)}')
print(f'低置信(<0.5): {sum(1 for i in anime if i.get("confidence",0)<0.5)}')
