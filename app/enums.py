from enum import Enum

class PaymentMethod(Enum):
    CASH = "CASH"      
    MPESA = "MPESA"    
    
    @property
    def display(self):
        return {
            "CASH": "Cash",
            "MPESA": "M-Pesa"
        }.get(self.value, self.value)

class AlertType(Enum):
    LOW_STOCK = 'low_stock'
    OUT_OF_STOCK = "out_of_stock"
    EXPIRING = "expiring_soon"
    EXPIRED = "expired"
    SLOW_MOVING = "slow_moving"
    RECALL = "recall"
    QUALITY_ISSUE = "quality_issue"
    PAYMENT = 'payment_reminder'
    REORDER = 'reorder'