
from odoo import fields, models, api


class StockMove(models.Model):
    _inherit = 'stock.move'

    # ipv_line_id = fields.Many2one('stock.ipv', ondelete='cascade')
