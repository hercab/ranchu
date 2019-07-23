
from odoo import fields, models, api


class StockMove(models.Model):
    _inherit = 'stock.move'

    ipvl_raw_material_id = fields.Many2one('stock.ipv.line', ondelete='cascade')
