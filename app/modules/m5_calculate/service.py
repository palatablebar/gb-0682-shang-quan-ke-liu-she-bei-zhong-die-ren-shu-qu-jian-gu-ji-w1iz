from app.modules.m5_calculate.rules import apply
from app.work_orders import build_work_orders

def execute(state):
    return build_work_orders(apply(state))
