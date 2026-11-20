from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Node:
    value: Optional[int]
    children: List["Node"] = field(default_factory=list)
