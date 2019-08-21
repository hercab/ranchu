
from itertools import groupby
from operator import itemgetter

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'
    _parent_store = True

    ipv_id = fields.Many2one('stock.ipv', string='IPV Reference', ondelete='cascade')

    product_id = fields.Many2one('product.product', 'Product', required=True,
                                 domain=[('type', 'in', ['product']), ('available_in_pos', '=', True),
                                         ],
                                 )
    bom_id = fields.Many2one('mrp.bom')
    product_uom = fields.Many2one('uom.uom', related='product_id.uom_id', readonly=True)

    parent_id = fields.Many2one('stock.ipv.line', 'Manufactured Product',
                                help='Product that is manufactured', ondelete='cascade')
    raw_ids = fields.One2many('stock.ipv.line', 'parent_id', 'Raw Materials',
                              help='Optional: Raw Materials for this Product'
                              )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting', 'Waiting Another Operation'),
        ('confirmed', 'Waiting'),
        ('assigned', 'Ready'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], compute="_compute_state", readonly=True, copy=False)

    is_locked = fields.Boolean('Is Locked?', compute='_compute_is_locked', readonly=True)

    is_manufactured = fields.Boolean('Is manufactured?')

    has_moves = fields.Boolean('Has moves?', compute='_compute_has_moves')

    move_ids = fields.One2many(comodel_name='stock.move',
                               inverse_name='ipvl_id',
                               string='Moves')

    initial_stock_qty = fields.Float('Initial Stock', readonly=True, copy=False)

    on_hand_qty = fields.Float('On Hand', compute='_compute_on_hand_qty', readonly=True,
                               help='Cantidad a mano en el area de venta, puede entrar la cantidad que desea tener')

    request_qty = fields.Float(string='Initial Demand', help='Cantidad que desea mover al area de venta')

    consumed_qty = fields.Float('Consumed', compute='_compute_consumed_qty', copy=False)

    _sql_constraints = [('unique_product_bom',
                         'unique(ipv_id, product_id, bom_id)',
                         'Cannot have duplicate product, please review and request quantity needed')]

    @api.onchange('product_id')
    def onchange_product_id(self):
        if not self.product_id:
            self.bom_id = False
            return {'domain': {
                'product_id': [('type', 'in', ['product']), ('available_in_pos', '=', True),
                               ('id', 'not in', self.ipv_id.ipv_lines.mapped('product_id').ids)]
            }}
        elif self.product_id.bom_count:
            self.is_manufactured = True
            bom = self.env['mrp.bom']._bom_find(product=self.product_id)
            if bom.type == 'normal':
                self.bom_id = bom.id
            else:
                self.bom_id = False
        else:
            self.is_manufactured = False
            self.bom_id = False

    @api.depends('product_id')
    def _compute_on_hand_qty(self):
        """Computa la cantidad de productos a mano en el area de venta, tiene que ser dependiente del contexto o
        calcular como init_stock + request_qty - consumed?"""
        for ipvl in self:
            for ipvl in self:
                ipvl.on_hand_qty = ipvl.product_id.with_context(
                    {'location': ipvl.parent_id.ipv_id.location_dest_id.id or ipvl.ipv_id.location_dest_id.id}).qty_available

    @api.depends('on_hand_qty')
    def _compute_consumed_qty(self):
        for ipvl in self:
            if ipvl.state in ['open', 'close']:
                ipvl.consumed_qty = (ipvl.initial_stock_qty + ipvl.request_qty) - ipvl.on_hand_qty
            else:
                ipvl.consumed_qty = 0.0

    @api.model
    def _compute_is_locked(self):
        for ipvl in self:
            ipvl.is_locked = ipvl.ipv_id.is_locked

    @api.depends('move_ids')
    def _compute_has_moves(self):
        for ipvl in self:
            ipvl.has_moves = bool(ipvl.move_ids)

    @api.depends('move_ids.state')
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
                moves = ipvl.raw_ids.mapped('move_ids')
                if not moves:
                    ipvl.state = 'draft'
                elif any(move.state == 'draft' for move in moves):  # TDE FIXME: should be all ?
                    ipvl.state = 'draft'
                elif all(move.state == 'cancel' for move in moves):
                    ipvl.state = 'cancel'
                elif all(move.state in ['cancel', 'done'] for move in moves):
                    ipvl.state = 'done'
                else:
                    relevant_move_state = moves._get_relevant_state_among_moves()
                    if relevant_move_state == 'partially_available':
                        ipvl.state = 'assigned'
                    else:
                        ipvl.state = relevant_move_state
            elif not ipvl.move_ids:
                ipvl.state = 'draft'
            else:
                ipvl.state = ipvl.move_ids.state

    @api.model
    def create(self, vals):
        if vals.get('is_manufactured'):
            vals['raw_ids'] = self.prepare_raw_materials(vals)
        res = super(StockIpvLine, self).create(vals)
        return res

    @api.model
    def write(self, vals):
        if 'product_id' in vals:
            self.raw_ids.unlink()
            if ('is_manufactured' in vals and vals.get('is_manufactured')) or\
               ('is_manufactured' not in vals and self.is_manufactured):
                vals['raw_ids'] = self.prepare_raw_materials(vals)
        elif vals.get('request_qty') and self.is_manufactured:
            qty = vals.get('request_qty')
            lines = self.explode_proportion(self.bom_id, self.product_id, qty)
            for boml, line_data in lines:
                self.raw_ids.filtered(lambda raw: raw.product_id.id == boml.product_id.id).update(
                    {'request_qty': line_data['qty']})
        return super(StockIpvLine, self).write(vals)

    def prepare_raw_materials(self, vals={}):
        bom = self.env['mrp.bom'].browse(vals.get('bom_id'))
        product = self.env['product.product'].browse(vals.get('product_id'))
        qty = vals.get('request_qty') if 'request_qty' in vals else self.request_qty
        lines = self.explode_proportion(bom, product, qty)
        raws = []
        for boml, line_data in lines:
            raws.append((0, 0, {
                'product_id': boml.product_id.id,
                'request_qty':  line_data['qty']
            }))
        return raws

    def explode_proportion(self, bom=None, product=None, quantity=0.0):
        # cantidad de veces que necesito la BoM
        factor = product.uom_id._compute_quantity(quantity, bom.product_uom_id) / bom.product_qty
        boms, lines = bom.explode(product, factor)
        return lines

    def _merge_raws_fields(self):
        """ This method will return a dict of stock move’s values that represent the values of all moves in `self` merged. """
        # state = self._get_relevant_state_among_moves()
        # origin = '/'.join(set(self.filtered(lambda m: m.origin).mapped('origin')))
        return {
            'product_id': self[0].product_id.id,
            'request_qty': sum(self.mapped('request_qty')),
            # 'date': min(self.mapped('date')),
            # 'date_expected': min(self.mapped('date_expected')) if self.mapped('picking_id').move_type == 'direct' else max(self.mapped('date_expected')),
            # 'move_dest_ids': [(4, m.id) for m in self.mapped('move_dest_ids')],
            # 'move_orig_ids': [(4, m.id) for m in self.mapped('move_orig_ids')],
            # 'state': state,
            # 'origin': origin,
        }

    @api.model
    def _prepare_merge_raws_distinct_fields(self):
        return [
            'product_id', 'bom_id'
        ]

    @api.model
    def _prepare_merge_raw_sort_method(self, ipvl_raw):
        ipvl_raw.ensure_one()
        return [
            ipvl_raw.product_id.id, ipvl_raw.bom_id.id
        ]

    def _clean_merged(self):
        """Cleanup hook used when merging moves"""
        self.write({'propagate': False})

    def merge_raws(self, merge_into=False):
        """ This method will, for each move in `self`, go up in their linked picking and try to
        find in their existing moves a candidate into which we can merge the move.
        :return: Recordset of moves passed to this method. If some of the passed moves were merged
        into another existing one, return this one and not the (now unlinked) original.
        """
        distinct_fields = self._prepare_merge_raws_distinct_fields()

        candidate_raws_list = []
        if not merge_into:
            candidate_raws_list.append(self)
        else:
            candidate_raws_list.append(merge_into | self)

        # Move removed after merge
        raws_to_unlink = self.env['stock.ipv.line']
        raws_to_merge = []
        for candidate_raws in candidate_raws_list:
            # First step find move to merge.
            candidate_raws = candidate_raws.with_context(prefetch_fields=False)
            for k, g in groupby(sorted(candidate_raws, key=self._prepare_merge_raw_sort_method),
                                key=itemgetter(*distinct_fields)):
                raws = self.env['stock.ipv.line'].concat(*g).filtered(lambda r: r.state not in ('done', 'cancel'))
                # If we have multiple records we will merge then in a single one.
                if len(raws) > 1:
                    raws_to_merge.append(raws)

        # # second step merge its move lines, initial demand, ...
        pure_raws = []
        for raws in raws_to_merge:
            #     # link all move lines to record 0 (the one we will keep).
            #     moves.mapped('move_line_ids').write({'move_id': moves[0].id})
            #     # merge move data
            raws[0].update(raws._merge_raws_fields())
        #     # update merged moves dicts
            raws_to_unlink |= raws[1:]
        #
        # if moves_to_unlink:
        #     # We are using propagate to False in order to not cancel destination moves merged in moves[0]
        #     moves_to_unlink._clean_merged()
        #     moves_to_unlink._action_cancel()
        #     moves_to_unlink.sudo().unlink()
        return (self | self.env['stock.ipv.line'].concat(*raws_to_merge)) - raws_to_unlink

    @api.multi
    def action_confirm(self):
        """ Confirma las movidas para los productos y materias primas"""
        ipvl_todo = self + self.mapped('raw_ids')

        ipvl_todo._generate_moves()
        # Forzar las movidas
        for move in ipvl_todo.filtered('is_manufactured').mapped('move_ids'):
            if move.is_quantity_done_editable:
                move.quantity_done = move.product_uom_qty
            else:
                for ml in move.move_line_ids:
                    ml.qty_done = ml.product_uom_qty

        # Confirmar las Movidas
        ipvl_todo.mapped('move_ids').filtered(lambda move: move.state == 'draft')._action_confirm()
        return ipvl_todo

    def _generate_moves(self):
        for ipvl in self:
            # original_quantity = (self.product_qty - self.qty_produced) or 1.0
            data = {
                # 'sequence': bom_line.sequence,
                'name': ipvl.parent_id.ipv_id.name or ipvl.ipv_id.name,
                # 'bom_line_id': bom_line.id,
                'picking_type_id': self.env.ref('stock_ipv.ipv_picking_type').id,
                'product_id': ipvl.product_id.id,
                'product_uom_qty': ipvl.request_qty,
                'product_uom': ipvl.product_uom.id,
                'location_id': ipvl.parent_id.ipv_id.location_id.id or ipvl.ipv_id.location_id.id,
                'location_dest_id': ipvl.parent_id.ipv_id.location_dest_id.id or ipvl.ipv_id.location_dest_id.id,
                # 'raw_material_production_id': self.id,
                # 'company_id': self.company_id.id,
                # 'operation_id': bom_line.operation_id.id or alt_op,
                # 'price_unit': bom_line.product_id.standard_price,
                # 'procure_method': 'make_to_stock',
                'origin': ipvl.parent_id.ipv_id.name or ipvl.ipv_id.name,
                'warehouse_id': ipvl.parent_id.ipv_id.location_id.get_warehouse().id or ipvl.ipv_id.location_id.get_warehouse().id,
                # 'group_id': self.procurement_group_id.id,
                # 'propagate': self.propagate,
                # 'unit_factor': quantity / original_quantity,
            }
            ipvl.move_ids = self.env['stock.move'].create(data)
