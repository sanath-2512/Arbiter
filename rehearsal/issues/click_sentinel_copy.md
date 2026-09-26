Pickling click parameters without an explicit default fails

We send click commands to worker processes with multiprocessing, which pickles them. Since upgrading click
this fails for every option or argument that has no explicit `default`:

```python
import pickle
import click

opt = click.Option(["--name"])
pickle.loads(pickle.dumps(opt))
# ValueError: <object object at 0x7f...> is not a valid Sentinel
```

`copy.copy` and `copy.deepcopy` of the same option work. Pickling should work too, and the unpickled
parameter should still mean "no default was given".
