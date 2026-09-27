#[derive(Default)]
pub struct Stack<T> {
    items: Vec<T>,
}

impl<T> Stack<T> {
    pub fn new() -> Self {
        Stack { items: Vec::new() }
    }
    pub fn push(&mut self, v: T) {
        self.items.push(v);
    }
    pub fn pop(&mut self) -> Option<T> {
        self.items.pop()
    }
    pub fn len(&self) -> usize {
        self.items.len()
    }
    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn push_pop() {
        let mut s = Stack::new(); s.push(2); assert_eq!(s.pop(), Some(2));
    }
}
