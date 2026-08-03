import sqlite3
c = sqlite3.connect(r'f:\xiangmu\zhongzhuan\data.db')
print("== api_keys schema ==")
for r in c.execute("PRAGMA table_info(api_keys)"):
    print(r)
print()
print("== api_keys sample ==")
for r in c.execute("SELECT * FROM api_keys LIMIT 3"):
    print(r)
print()
print("== models schema ==")
for r in c.execute("PRAGMA table_info(models)"):
    print(r)
print()
print("== models sample ==")
for r in c.execute("SELECT * FROM models"):
    print(r)
