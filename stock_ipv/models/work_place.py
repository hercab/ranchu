
from odoo import models, fields, api


class WorkPlace(models.Model):
    _name = 'ipv.work.place'
    _description = 'IPV Work Place'

    name = fields.Char('Name', required=True)

    stock_loc = fields.Many2one('stock.location', 'Stock',
                                domain=[('usage', 'in', ['internal'])],
                                default=lambda self: self.env.ref('stock.stock_location_stock').id,
                                required=True,
                                )

    elaboration_loc = fields.Many2one('stock.location', 'Elaboration Area',
                                      domain=[('usage', 'in', ['internal'])],
                                      default=lambda self: self.env.ref('stock_ipv.ipv_location_elaboration').id,
                                      required=True,
                                      )
    sales_loc = fields.Many2one('stock.location', string='Sales Area',
                                domain=[('usage', 'in', ['internal'])],
                                default=lambda self: self.env.ref('stock_ipv.ipv_location_sales').id,
                                required=True,
                                )
    product_tmpl_ids = fields.Many2many('product.template')
    # ipv_ids = fields.One2many('stock.ipv', 'workplace_id')

    # pos_id = fields.Many2one('pos.config', string='POS', default=lambda s: s.env.ref('point_of_sale.pos_config_main'))
