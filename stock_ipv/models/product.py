
from odoo import models, fields, api


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    workplace_ids = fields.Many2many('ipv.work.place', string='Sales Place', help='Place where this product can be sales')
    elaboration_loc = fields.Many2one('stock.location', 'Elaboration Area',
                                      domain=[('usage', 'in', ['internal'])],
                                      help='Location overwrite where this product is elaborated'
                                      )
