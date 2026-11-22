from flask import Blueprint
from app.web import api,page
from app import store
bp=Blueprint('m5_calculate',__name__)
@bp.get('/m5_calculate')
def screen():return page(5,'m5_calculate.html',{'work_orders':store.list_work_orders()})
@bp.route('/api/m5_calculate',methods=['GET','POST'])
def preview():return api(5)
