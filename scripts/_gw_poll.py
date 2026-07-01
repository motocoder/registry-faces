import sys, happybase
try:
    c = happybase.Connection(host='127.0.0.1', port=9090, timeout=8000)
    t = c.table('person')
    n = 0
    for k, d in t.scan(limit=3, batch_size=3):
        n += 1
        break
    c.close()
    print('READY')
    sys.exit(0)
except Exception as e:
    print('not-ready:', type(e).__name__)
    sys.exit(1)
