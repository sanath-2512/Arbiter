GenericIPAddressField should accept ipaddress.IPv4Address / IPv6Address objects

`GenericIPAddressField.to_python()` already accepts `ipaddress` objects, but saving or querying with one
crashes:

```python
from ipaddress import IPv4Address
Host.objects.create(ip=IPv4Address("192.0.2.1"))
# TypeError: argument of type 'IPv4Address' is not iterable
Host.objects.filter(ip=IPv4Address("192.0.2.1"))   # same error
```

It would be nice if creating, updating and filtering accepted `IPv4Address` and `IPv6Address` values
directly and stored/compared them exactly like their string form (the loaded value is a string, as today).
