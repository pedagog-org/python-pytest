"""A student's linked list with the mistakes the corpus needs."""


class _Node:
    def __init__(self, value, next=None):
        self.value = value
        self.next = next


class LinkedList:
    def __init__(self):
        self.head = None
        self.size = 0

    def append(self, value):
        node = _Node(value)
        if self.head is None:
            self.head = node
        else:
            cur = self.head
            while cur.next is not None:
                cur = cur.next
            cur.next = node
        # Bug: never counts the first element.
        if self.head is not node:
            self.size += 1

    def __len__(self):
        return self.size

    def pop_front(self):
        # Bug: no empty check, so an empty list raises AttributeError.
        value = self.head.value
        self.head = self.head.next
        return value

    def remove(self, value):
        # Bug: a missing value is silently ignored instead of raising ValueError.
        prev, cur = None, self.head
        while cur is not None and cur.value != value:
            prev, cur = cur, cur.next
        if cur is None:
            return
        if prev is None:
            self.head = cur.next
        else:
            prev.next = cur.next

    def find(self, value):
        return self._find(self.head, value)

    def _find(self, node, value):
        # Bug: never stops at the end.
        if node.value == value:
            return node
        return self._find(node.next, value)

    def to_list(self):
        out = []
        cur = self.head
        while cur is not None:
            out.insert(0, cur.value)  # Bug: reversed.
            cur = cur.next
        return out

    def show(self):
        print(" -> ".join(str(v) for v in self.to_list()))

    def spin(self):
        while True:
            pass
