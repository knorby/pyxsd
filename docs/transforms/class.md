# The Transform class

`pyxsd.transforms.Transform` is the abstract base every transform inherits
from. It provides tree access and node-manufacturing helpers; anything you
add to it must operate only on the generic node shape so it applies to all
transforms.

```{eval-rst}
.. autoclass:: pyxsd.transforms.transform.Transform
   :members:
   :undoc-members:
```

```{eval-rst}
.. autoclass:: pyxsd.transforms.displayer.Displayer
   :members:
   :undoc-members:
```

## Tree iteration

```{eval-rst}
.. autofunction:: pyxsd.transforms.transform.iter_tree
```

## Built-in transforms

```{eval-rst}
.. autoclass:: pyxsd.transforms.print_data.PrintData
   :members:
```
