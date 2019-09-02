
from itertools import groupby
from operator import itemgetter

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'

    ipv_id = fields.Many2one('stock.ipv', string='IPV Reference', ondelete='cascade')

    product_id = fields.Many2one('product.product', 'Product', required=True,
                                 domain=[('type', 'in', ['product']), ('available_in_pos', '=', True),
                                         ],
                                 )
    elaboration_loc = fields.Many2one('stock.location', related='product_id.elaboration_loc')

    is_manufactured = fields.Integer(related='product_id.bom_count')

    is_raw = fields.Boolean(string='Is Raw Material')

    bom_id = fields.Many2one('mrp.bom', string='Bill of Materials')

    product_uom = fields.Many2one('uom.uom', related='product_id.uom_id', readonly=True)

    parent_ids = fields.Many2many('stock.ipv.line', relation='stock_ipv_product_raws_rel', column1='parent_id', column2='raw_id',
                                  string='Manufactures', help='Product that is manufactured')
    raw_ids = fields.Many2many('stock.ipv.line', relation='stock_ipv_product_raws_rel', column2='parent_id', column1='raw_id',
                               help='Raw Materials for this Product'
                               )

    state = fields.Selection([
        ('draft', 'New'), ('cancel', 'Cancelled'),
        ('waiting', 'Waiting Another Move'),
        ('confirmed', 'Waiting Availability'),
        ('partially_available', 'Partially Available'),
        ('assigned', 'Available'),
        ('done', 'Done')], string='Status',
        copy=False, compute='_compute_state', store=False, default='draft', index=True, readonly=True,
        help="* New: When the stock move is created and not yet confirmed.\n"
             "* Waiting Another Move: This state can be seen when a move is waiting for another one, for example in a chained flow.\n"
             "* Waiting Availability: This state is reached when the procurement resolution is not straight forward. It may need the scheduler to run, a component to be manufactured...\n"
             "* Available: When products are reserved, it is set to \'Available\'.\n"
             "* Done: When the shipment is processed, the state is \'Done\'.")

    # is_locked = fields.Boolean('Is Locked?', compute='_compute_is_locked', readonly=True)

    saleable_in_pos = fields.Boolean(related='product_id.available_in_pos')

    has_moves = fields.Boolean('Has moves?', compute='_compute_has_moves')

    move_ids = fields.One2many('stock.move', 'ipvl_id', string='Moves')

    string_availability_info = fields.Text(related='move_ids.string_availability_info')

    initial_stock_qty = fields.Float('Initial Stock', readonly=True, copy=False)

    on_hand_qty = fields.Float('On Hand', compute='_compute_on_hand_qty', readonly=False,
                               help='Cantidad Disponible, En estado draft puede entrar la cantidad que desea tener')

    request_qty = fields.Float(string='Demand', help='Cantidad que desea mover al area de ventas')

    consumed_qty = fields.Float('Consumed', compute='_compute_consumed_qty', copy=False)

    @api.multi
    def name_get(self):
        result = []
        for ipvl in self:
            name = '%s (%s)' % (ipvl.product_id.name, ipvl.request_qty)
            result.append((ipvl.id, name))
        return result

    # _sql_constraints = [('unique_product_bom',
    #                      'unique(ipv_id, product_id, bom_id)',
    #                      'Cannot have duplicate product, please review and request quantity needed')]

    @api.onchange('product_id')
    def onchange_product_id(self):
        if not self.product_id:
            self.bom_id = False
            result = {}
            if self.ipv_id.workplace_id:
                result['domain'] = {'product_id': ['|', '&', '&', '&', ('type', 'in', ['product']), ('available_in_pos', '=', True),
                                    ('id', 'not in', self.ipv_id.saleable_lines.mapped('product_id').ids),
                                    ('workplace_ids', '=', False), ('product_tmpl_id', 'in', self.ipv_id.workplace_id.product_tmpl_ids.ids)]}
            else:
                result['warning'] = {'title': 'Work Place not Set',
                                     'message': 'Please select The Work Place'}

            return result
        elif self.product_id.bom_count:
            bom = self.env['mrp.bom']._bom_find(product=self.product_id)
            if bom.type == 'normal':
                self.bom_id = bom.id
            else:
                self.bom_id = False
        else:
            self.bom_id = False

    @api.depends('product_id')
    def _compute_on_hand_qty(self):
        """Computa la cantidad de productos a mano en el area de venta, tiene que ser dependiente del contexto o
        calcular como init_stock + request_qty - consumed?"""
        for ipvl in self:
            if ipvl.saleable_in_pos and not ipvl.is_raw:
                location = ipvl.ipv_id.workplace_id.sales_loc
            elif ipvl.elaboration_loc:
                location = ipvl.elaboration_loc
            else:
                location = ipvl.ipv_id.workplace_id.elaboration_loc

            ipvl.on_hand_qty = ipvl.product_id.with_context(
                    {'location': location.id}).qty_available

    @api.depends('on_hand_qty')
    def _compute_consumed_qty(self):
        for ipvl in self:
            ipvl.consumed_qty = (ipvl.initial_stock_qty + ipvl.request_qty) - ipvl.on_hand_qty

    # @api.model
    # def _compute_is_locked(self):
    #     for ipvl in self:
    #         ipvl.is_locked = ipvl.ipv_id.is_locked
    #

    @api.depends('move_ids')
    def _compute_has_moves(self):
        for ipvl in self:
            ipvl.has_moves = bool(ipvl.move_ids)

    @api.depends('raw_ids.state', 'move_ids.state')
    def _compute_state(self):
        for ipvl in self:
            ''' State of a picking depends on the state of its related stock.move
                    - Draft: only used for "planned pickings"
                    - Waiting: if the picking is not ready to be sent so if
                      - (a) no quantity could be reserved at all or if
                      - (b) some quantities could be reserved and the shipping policy is "deliver all at once"
                    - Waiting another move: if the picking is waiting for another move
                    - Ready: if the picking is ready to be sent so if:
                      - (a) all quantities are reserved or if
                      - (b) some quantities could be reserved and the shipping policy is "as soon as possible"
                    - Done: if the picking is done.
                    - Cancelled: if the picking is cancelled
                    '''
            if ipvl.is_manufactured:
                if all(raw.state == 'draft' for raw in ipvl.raw_ids):
                    ipvl.state = 'draft'
                elif all(raw.state == 'cancel' for raw in ipvl.raw_ids):
                    ipvl.state = 'cancel'
                elif all(raw.state in ['done'] for raw in ipvl.raw_ids):
                    ipvl.state = 'done'
                else:
                    moves = ipvl.mapped('raw_ids.move_ids')
                    relevant_move_state = moves._get_relevant_state_among_moves()
                    if relevant_move_state == 'partially_available':
                        ipvl.state = 'assigned'
                    else:
                        ipvl.state = relevant_move_state
            elif not ipvl.has_moves:
                ipvl.state = 'draft'
            else:
                ipvl.state = ipvl.move_ids.state

    @api.model
    def create(self, vals):
        res = super(StockIpvLine, self).create(vals)
        if res.is_manufactured:
            res.prepare_raw_materials()
        return res

    def unlink(self):
        for ipvl in self.filtered('is_manufactured'):
            ipvl.raw_ids.filtered(lambda r: len(r.parent_ids) == 1).unlink()
            ipvl.update_request_qty()
        move_to_unlink = self.mapped('move_ids')
        # Pueden quedar picking sin movidas
        if move_to_unlink:
            move_to_unlink._action_cancel()
            move_to_unlink.sudo().unlink()
        return super(StockIpvLine, self).unlink()

    @api.model
    def write(self, vals):
        if 'product_id' in vals and self.is_manufactured:
            self.raw_ids.filtered(lambda r: len(r.parent_ids) == 1).unlink()
            # Update Share Raws
            if self.raw_ids:
                self.update_request_qty()
                self.raw_ids.update({'parent_ids': [(3, self.id)]})
        elif 'request_qty' in vals:
            qty = vals.get('request_qty')
            self.update_request_qty(qty)
        super(StockIpvLine, self).write(vals)
        if vals.get('bom_id'):
            self.prepare_raw_materials()

    def prepare_raw_materials(self):
        info = {
            'parent_ids': [(4, self.id)]
        }
        if self.request_qty:
            raws, lines = self.explode_proportion(self.request_qty)
            info['request_qty'] = 0.0
        for boml in self.bom_id.bom_line_ids:
            if 'request_qty' in info:
                info['request_qty'] = raws.get(boml.product_id.name)
            raw_existent = self.ipv_id.raw_lines.filtered(lambda r: r.product_id == boml.product_id
                                                          and r.elaboration_loc == self.elaboration_loc)
            if raw_existent:
                if 'request_qty' in info:
                    info['request_qty'] += raw_existent.request_qty
                raw_existent.write(info)
            else:
                info.update({
                    'ipv_id': self.ipv_id.id,
                    'is_raw': True,
                    'product_id': boml.product_id.id,
                    'elaboration_loc': self.elaboration_loc.id,
                })
                self.env['stock.ipv.line'].create(info)
        return True

    def explode_proportion(self, quantity=0.0):
        # cantidad de veces que necesito la BoM
        bom = self.bom_id
        raws = {}
        factor = self.product_id.uom_id._compute_quantity(quantity, bom.product_uom_id) / bom.product_qty
        boms, lines = bom.explode(self.product_id, factor)
        for line in lines:
            boml, data = line
            raws[boml.product_id.name] = data['qty']
        return raws, lines

    def update_request_qty(self, new_qty=0.0):
        self.ensure_one()
        ipv_id = self.ipv_id
        workplace_id = ipv_id.workplace_id
        dif_qty = new_qty - self.request_qty
        if dif_qty < 0.0 and self.state == 'done':
            raise UserError(_('You cannot reduce a qty that has been set to \'Done\'.'))
        if self.is_manufactured:
            old, lines = self.explode_proportion(self.request_qty)
            new, lines = self.explode_proportion(new_qty)
            for raw in self.raw_ids:
                old_request_qty = old.get(raw.product_id.name)
                new_request_qty = new.get(raw.product_id.name)
                raw.update({'request_qty': raw.request_qty - old_request_qty + new_request_qty})
        elif self.has_moves:
            if self.is_raw:
                dest_loc = self.elaboration_loc or workplace_id.elaboration_loc
            else:
                dest_loc = workplace_id.sales_loc

            move = self.env['stock.move'].create({
                'name': '%s(%s)' % (self.ipv_id.name, self.product_id.name),
                'ipvl_id': self.id,
                'picking_type_id': self.env.ref('stock_ipv.ipv_picking_type').id,
                'product_id': self.product_id.id,
                'product_uom_qty': dif_qty,
                'product_uom': self.product_uom.id,
                'location_id': ipv_id.workplace_id.stock_loc.id,
                'location_dest_id': dest_loc.id,
                # 'procure_method': 'make_to_stock',
                'origin': ipv_id.name,
                # 'warehouse_id': self.workplace_id.stock_loc.get_warehouse().id,
                'group_id': ipv_id.procurement_group_id.id

            })
            move._action_confirm(merge_into=(self.move_ids - move))
        return True


