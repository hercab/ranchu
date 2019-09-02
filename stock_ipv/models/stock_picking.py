
from odoo import models, fields, api


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    ipv_id = fields.Many2one('stock.ipv', ondelete='cascade')
