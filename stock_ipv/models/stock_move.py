from odoo import models, fields, api


class StockMove(models.Model):
    _inherit = 'stock.move'

    ipvl_id = fields.Many2one('stock.ipv.line', ondelete='cascade')