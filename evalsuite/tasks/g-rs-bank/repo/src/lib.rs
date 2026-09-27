#[derive(Debug, PartialEq)]
pub enum Error {
    InsufficientFunds,
}

pub struct Account {
    pub balance: i64,
}

impl Account {
    pub fn withdraw(&mut self, amount: i64) -> Result<i64, Error> {
        self.balance -= amount;
        Ok(self.balance)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn withdraw() {
        let mut a = Account { balance: 10 }; assert_eq!(a.withdraw(3), Ok(7));
    }
}
