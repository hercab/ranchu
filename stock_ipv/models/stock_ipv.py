# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError


class StockIpv(models.Model):
    _name = 'stock.ipv'
    _description = 'Stock IPV'
    _order = 'create_date desc'

    @api.model
    def default_get(self, fields):
        res = super(StockIpv, self).default_get(fields)
        if 'location_dest_id' in res:
            location_dest = self.env['stock.location'].browse([res['location_dest_id']])
            Quant = self.env['stock.quant']
            sublocations = location_dest.child_ids
            temp_ipvl = []
            for sublocation in sublocations:
                product = self.env['product.product'].search([('name', '=', sublocation.name)], limit=1)
                if sublocation.quant_ids:
                    ipvl = self.env['stock.ipv.line'].create({
                        'product_id': product.id,
                    })
                    temp_ipvl += [ipvl.id]
                if sublocation.usage == 'production':
                    # Hacer algo con los raw para mostrarlos tambien
                    raw_product_ids = sublocation.quant_ids.mapped('product_id')

                available_quantity = Quant._get_available_quantity(product, sublocation)

            res['ipv_lines'] = temp_ipvl

        return res

    name = fields.Char(required=True, copy=False, default='New')

    requested_by = fields.Many2one(
        'res.users', 'Requested by', required=True,
        default=lambda s: s.env.uid, readonly=True
    )

    location_id = fields.Many2one('stock.location', 'Origen',
                                  domain=[('usage', 'in', ['internal'])],
                                  default=lambda self: self.env.ref('stock.stock_location_stock').id,
                                  required=True,
                                  readonly=True,
                                  states={'draft': [('readonly', False)]}
                                  )

    # FIXME "Si cambia el Destino deberia reflejarse en todas las move not done"
    location_dest_id = fields.Many2one('stock.location', 'Destino',
                                       domain=[('usage', 'in', ['internal'])],
                                       default=lambda self: self.env.ref('stock_ipv.ipv_location_destiny').id,
                                       required=True,
                                       readonly=True,
                                       states={'draft': [('readonly', False)]},
                                       help='Stock Destiny Location',
                                       )

    state = fields.Selection([
        ('draft', 'Draft'),
        ('check', 'Check'),
        ('assign', 'Ready'),
        ('open', 'Open'),
        ('close', 'Close'),
        ('cancel', 'Cancelled'),
    ], string='Status', compute='_compute_state',
        copy=False, index=True, readonly=True, store=True, track_visibility='onchange',
        help=" * Draft: No ha sido confirmado.\n"
             " * Ready: Chequeado disponibilidad y reservada las cantidades, listo para ser abierto.\n"
             " * Open: Las cantidades han sido movidas, no hay retorno, se puede agregar mas cantidades y productos.\n"
             " * Close: Esta bloqueado y no se puede editar mas.\n")

    ipv_lines = fields.One2many('stock.ipv.line', 'ipv_id')

    raw_quants = fields.One2many('stock.quant',
                                 compute='_compute_raw_quants',
                                 )

    show_check_availability = fields.Boolean(
        compute='_compute_show_check_availability',
        help='Technical field used to compute whether the check availability button should be shown.')

    show_validate = fields.Boolean(
        compute='_compute_show_validate',
        help='Technical field used to compute whether the validate should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_open = fields.Datetime('Open date', copy=False, readonly=True,
                                help="Date at which the turn has been processed or cancelled.")

    date_close = fields.Datetime('Close date', copy=False, readonly=True,
                                help="Date at which the turn was closed.")

    @api.depends('ipv_lines', 'location_dest_id')
    def _compute_raw_quants(self):
        self.raw_quants = self.ipv_lines.mapped('sublocation_id').\
            filtered(lambda s: s.usage == 'production').mapped('quant_ids')

    @api.depends('ipv_lines.state')
    def _compute_state(self):
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
        - Cancelled: if the picking is cancelled '''

        if not self.ipv_lines:
            self.state = 'draft'
        elif all(ipvl.state == 'draft' for ipvl in self.ipv_lines):
            self.state = 'draft'
        elif all(ipvl.state in ['done'] for ipvl in self.ipv_lines):
            self.state = 'open'
        elif all(ipvl.state in ['|', 'assigned', 'partially_available'] for ipvl in self.ipv_lines):
            self.state = 'assign'
        else:
            self.state = 'check'

    @api.depends('ipv_lines.product_uom_qty', 'state')
    def _compute_show_check_availability(self):
        self.ensure_one()
        has_moves_to_reserve = any(float_compare(ipvl.product_uom_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                   and ipvl.state not in ['assigned', 'done']
                                   for ipvl in self.ipv_lines
                                   )

        self.show_check_availability = self.is_locked and self.state in (
                'draft', 'check') and has_moves_to_reserve

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_validate(self):
        for ipv in self:
            if ipv.state == 'draft':
                ipv.show_validate = False
            elif ipv.state not in ('check', 'assign', 'open'):
                ipv.show_validate = False
            else:
                ipv.show_validate = True

    @api.model
    def create(self, vals):
        if vals.get('name', 'New') == 'New':
            vals['name'] = self.env['ir.sequence'].next_by_code('stock.ipv.seq')
        res = super().create(vals)
        return res

    def unlink(self):
        for ipv in self:
            if ipv.state not in ['draft', 'cancel']:
                raise UserError('No puede borrar un IPV que no este cancelado.')
            ipv.mapped('ipv_lines').unlink()
        return super(StockIpv, self).unlink()

    @api.multi
    def action_confirm(self):
        # call `_action_confirm` on every draft ipv line
        self.ipv_lines.action_confirm()
        return True

    @api.multi
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        self.filtered(lambda ipv: ipv.state == 'draft').action_confirm()
        self.ipv_lines.action_assign()
        return True

    @api.multi
    def action_done(self):
        """"Changes picking state to done by processing the Stock Moves of the Picking

        Normally that happens when the button "Done" is pressed on a Picking view.
        @return: True
        """
        self.ipv_lines.action_done()

        self.write({'date_done': fields.Datetime.now()})
        return True

    @api.multi
    def do_unreserve(self):
        for ipvl in self:
            ipvl.move_ids._do_unreserve()

    @api.multi
    def button_validate(self):
        self.ensure_one()
        self.ipv_lines.action_validate()
        self.action_done()
        return True

    @api.multi
    def button_close(self):
        return True


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'
    _parent_store = True

    ipv_id = fields.Many2one('stock.ipv',
                             string='Ipv Reference')

    parent_id = fields.Many2one('stock.ipv.line', 'Parent IPV line', ondelete='restrict')

    parent_path = fields.Char(index=True)

    child_ids = fields.One2many('stock.ipv.line', 'parent_id', 'Raw Material lines',
                                compute='_compute_child_ids')

    move_ids = fields.One2many(comodel_name='stock.move',
                               inverse_name='ipvl_id',
                               string='Ipvl Moves')

    on_hand_qty = fields.Float('On Hand',
                               compute='_compute_on_hand_qty',
                               readonly=True)

    initial_stock_qty = fields.Float('Initial Stock',
                                     readonly=True)

    consumed_qty = fields.Float('Consumed', compute='_compute_consumed_qty', store=True)

    product_id = fields.Many2one('product.product', 'Product', required=True,
                                 states={'assigned': [('readonly', True)],
                                         'done': [('readonly', True)]},
                                 )

    sublocation_id = fields.Many2one('stock.location',
                                     compute='_compute_sublocation',
                                     readonly=True,
                                     store=True
                                     )

    state = fields.Selection([
                            ('draft', 'Draft'),
                            ('waiting', 'Waiting Another Operation'),
                            ('confirmed', 'Waiting'),
                            ('assigned', 'Ready'),
                            ('done', 'Done'),
                            ('cancel', 'Cancelled'),
                             ],
                             compute="_compute_state",
                             readonly=True)

    # related fields to stock.move
    product_uom_qty = fields.Float(string='Demanda Inicial')
    product_uom = fields.Many2one('uom.uom', related='product_id.uom_id', readonly=True)

    is_locked = fields.Boolean(compute='_compute_is_locked', readonly=True)

    is_manufactured = fields.Boolean('Is manufacture', compute="_compute_is_manufactured", store=True)
    has_moves = fields.Boolean('Has move?', compute='_compute_has_moves')

    @api.depends('product_id', 'ipv_id.location_dest_id')
    def _compute_sublocation(self):
        for ipvl in self:
            if ipvl.parent_id:
                location = ipvl.parent_id.sublocation_id
            else:
                location = ipvl.ipv_id.location_dest_id

            sublocation = self.env['stock.location'].search(['&', ('name', '=', ipvl.product_id.name),
                                                                  ('location_id', '=', location.id)])
            if sublocation:
                ipvl.sublocation_id = sublocation

            else:
                ipvl.sublocation_id = self.env['stock.location'].create({
                        'name': ipvl.product_id.name,
                        'usage': 'production' if ipvl.is_manufactured else 'transit',
                        'location_id': location.id,
                    })

    @api.depends('product_id')
    def _compute_child_ids(self):
        for ipvl in self:
            if ipvl.is_manufactured:
                bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)
                ipvls = []
                for boml in bom.bom_line_ids:
                    ipvls += [{
                        'parent_id': ipvl.id,
                        'product_id': boml.product_id.id,
                    }]
                ipvl.child_ids = self.env['stock.ipv.line'].create(ipvls)

    @api.depends('product_id', 'ipv_id.location_dest_id')
    def _compute_on_hand_qty(self):
        for ipvl in self:
            if ipvl.is_manufactured:
                bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)
                production = []
                for boml in bom.bom_line_ids:
                    boml_qty = boml.product_id.with_context(
                        {'location': ipvl.sublocation_id.id}).qty_available
                    prod = bom.product_qty * boml_qty / boml.product_qty
                    production_uom = bom.product_uom_id._compute_quantity(prod, ipvl.product_uom)
                    production += [production_uom]
                ipvl.on_hand_qty = min(production)

            else:
                ipvl.on_hand_qty = ipvl.product_id.with_context(
                    {'location': ipvl.sublocation_id.id}).qty_available

    @api.depends('on_hand_qty')
    def _compute_consumed_qty(self):
        for ipvl in self:
            ipvl.consumed_qty = (ipvl.initial_stock_qty + ipvl.product_uom_qty) - ipvl.on_hand_qty

    @api.model
    def _compute_is_locked(self):
        for ipvl in self:
            if ipvl.ipv_id:
                ipvl.is_locked = ipvl.ipv_id.is_locked

    @api.depends('product_id')
    def _compute_is_manufactured(self):
        for ipvl in self:
            if ipvl.product_id.bom_count:
                ipvl.is_manufactured = True
            else:
                ipvl.is_manufactured = False

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
                if not ipvl.move_ids:
                    ipvl.state = 'draft'
                elif any(move.state == 'draft' for move in ipvl.move_ids):  # TDE FIXME: should be all ?
                    ipvl.state = 'draft'
                elif all(move.state == 'cancel' for move in ipvl.move_ids):
                    ipvl.state = 'cancel'
                elif all(move.state in ['cancel', 'done'] for move in ipvl.move_ids):
                    ipvl.state = 'done'
                else:
                    relevant_move_state = ipvl.move_ids._get_relevant_state_among_moves()
                    if relevant_move_state == 'partially_available':
                        ipvl.state = 'assigned'
                    else:
                        ipvl.state = relevant_move_state

    @api.multi
    def unlink(self):
        for ipvl in self:
            ipvl.move_ids.unlink()
        return super().unlink()

    @api.multi
    def action_confirm(self):
        """ Crea las sublocations las movidas de materias primas o de mercancias y las confirma """
        for ipvl in self:
            if not ipvl.has_moves and ipvl.product_uom_qty:

                if ipvl.is_manufactured:
                    bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)

                    factor = ipvl.product_uom._compute_quantity(ipvl.product_uom_qty, bom.product_uom_id) / bom.product_qty
                    boms, lines = bom.explode(ipvl.product_id, factor)

                    ipvl.move_ids = ipvl._generate_raw_moves(lines)
                    # Check for all draft moves whether they are mto or not
                    # production._adjust_procure_method()

                else:
                    move_tmpl = {
                        'name': ipvl.ipv_id.name,
                        'product_id': ipvl.product_id.id,
                        'product_uom': ipvl.product_uom.id,
                        'product_uom_qty': ipvl.product_uom_qty,
                        'location_id': ipvl.ipv_id.location_id.id,
                        'location_dest_id': ipvl.sublocation_id.id
                        }
                    ipvl.move_ids = self.env['stock.move'].create(move_tmpl)
        self.mapped('move_ids').filtered(lambda move: move.state == 'draft')._action_confirm()
        return True

    def _generate_raw_moves(self, exploded_lines):
        self.ensure_one()
        moves = self.env['stock.move']

        for bom_line, line_data in exploded_lines:
            moves += self._generate_raw_move(bom_line, line_data)
        return moves

    def _generate_raw_move(self, bom_line, line_data):
        quantity = line_data['qty']
        # alt_op needed for the case when you explode phantom bom and all the lines will be consumed in the operation given by the parent bom line
        alt_op = line_data['parent_line'] and line_data['parent_line'].operation_id.id or False
        if bom_line.child_bom_id and bom_line.child_bom_id.type == 'phantom':
            return self.env['stock.move']
        if bom_line.product_id.type not in ['product', 'consu']:
            return self.env['stock.move']

        # original_quantity = (self.product_qty - self.qty_produced) or 1.0
        data = {
            'sequence': bom_line.sequence,
            'name': self.ipv_id.name,
            # 'date': self.date_planned_start,
            # 'date_expected': self.date_planned_start,
            'bom_line_id': bom_line.id,
            # 'picking_type_id': self.picking_type_id.id,
            'product_id': bom_line.product_id.id,
            'product_uom_qty': quantity,
            'product_uom': bom_line.product_uom_id.id,
            'location_id': self.ipv_id.location_id.id,
            'location_dest_id': self.sublocation_id.id,
            # 'raw_material_production_id': self.id,
            # 'company_id': self.company_id.id,
            'operation_id': bom_line.operation_id.id or alt_op,
            'price_unit': bom_line.product_id.standard_price,
            'procure_method': 'make_to_stock',
            # 'origin': self.name,
            'warehouse_id': self.ipv_id.location_id.get_warehouse().id,
            # 'group_id': self.procurement_group_id.id,
            # 'propagate': self.propagate,
            # 'unit_factor': quantity / original_quantity,
        }
        return self.env['stock.move'].create(data)

    @api.multi
    def action_assign(self):
        moves_to_check = self.mapped('move_ids').filtered(lambda move: move.state not in ('draft', 'cancel', 'done'))

        if not moves_to_check:
            raise UserError('Nothing to check the availability for.')
        for move in moves_to_check:
            move._action_assign()

    @api.multi
    def action_done(self):
        for ipvl in self:
            ipvl.write({
                'initial_stock_qty': ipvl.on_hand_qty
            })
        # TDE FIXME: remove decorator when migration the remaining
        todo_moves = self.mapped('move_ids').filtered(
            lambda move: move.state in ['draft', 'waiting', 'partially_available', 'assigned', 'confirmed'])

        todo_moves._action_done()

        return True

    @api.multi
    def action_validate(self):
        move_lines = self.mapped('move_ids')
        move_line_ids = move_lines.mapped('move_line_ids')
        if not move_lines and not move_line_ids:
            raise UserError('Please add some items to move.')

        precision_digits = self.env['decimal.precision'].precision_get('Product Unit of Measure')
        no_quantities_done = all(float_is_zero(move_line.qty_done, precision_digits=precision_digits) for move_line in
                                 move_line_ids.filtered(lambda m: m.state not in ('done', 'cancel')))
        no_reserved_quantities = all(
            float_is_zero(move_line.product_qty, precision_rounding=move_line.product_uom_id.rounding) for move_line in
            move_line_ids)
        if no_reserved_quantities and no_quantities_done:
            raise UserError(
                'You cannot validate a transfer if no quantites are reserved nor done. To force the transfer, switch in edit more and encode the done quantities.')
        if no_quantities_done:
            for move_line in move_line_ids:
                move_line.qty_done = move_line.product_uom_qty
        return True
