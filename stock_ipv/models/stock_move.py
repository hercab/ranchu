
from odoo import fields, models, api


class StockMove(models.Model):
    _inherit = 'stock.move'

    ipv_id = fields.Many2one('stock.ipv',
                             ondelete='cascade')

    stock_location_init_qty = fields.Float('Init Stock',
                                           compute='_compute_stock_location_init_qty',
                                           readonly=True)

    @api.depends('product_id', 'ipv_id.location_dest_id')
    def _compute_stock_location_init_qty(self):
        self.stock_location_init_qty = self.product_id.with_context({'location': self.ipv_id.location_dest_id.id}).qty_available
