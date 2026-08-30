# Sync guide.html into marchesini-collection
with open("/Users/lavoro/Documents/Enchanté/Conversations/5A73FD1A-410D-44BE-857C-BDE072721A54/guide.html", "r", encoding="utf-8") as f:
    code = f.read()

with open("/Users/lavoro/Documents/Enchanté/Conversations/2971D6C8-3946-418E-88C1-ACB07772E3FD/marchesini-collection/guide.html", "w", encoding="utf-8") as f:
    f.write(code)

print("SYNC OK")
