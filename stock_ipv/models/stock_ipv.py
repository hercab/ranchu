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

    raw_lines = fields.One2many('stock.ipv.line',
                                compute='_compute_raw_lines',
                                )

    show_check_availability = fields.Boolean(
        compute='_compute_show_check_availability',
        help='Technical field used to compute whether the check availability button should be shown.')

    show_validate = fields.Boolean(
        compute='_compute_show_validate',
        help='Technical field used to compute whether the validate should be shown.')

    show_open = fields.Boolean(
        compute='_compute_show_open',
        help='Technical field used to compute whether the validate should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_open = fields.Datetime('Open date', copy=False, readonly=True,
                                help="Date at which the turn has been processed or cancelled.")

    date_close = fields.Datetime('Close date', copy=False, readonly=True,
                                 help="Date at which the turn was closed.")

    @api.depends('ipv_lines')
    def _compute_raw_lines(self):
        self.raw_lines = self.ipv_lines.mapped('child_ids')

    @api.onchange('location_dest_id')
    def _compute_child_lines(self):
        dest = self.location_dest_id
        IPVl = self.env['stock.ipv.line']
        # Quant = self.env['stock.quant']
        sublocations = dest.child_ids
        self.ipv_lines = []
        for sublocation in sublocations:
            product = self.env['product.product'].search([('name', '=', sublocation.name)], limit=1)

            if sublocation.quant_ids:
                data = {
                    'ipv_id': self.id,
                    'product_id': product.id,
                    'child_ids': []
                }
                if sublocation.usage == 'production':
                    for quant in sublocation.quant_ids:
                        child = {
                            'product_id': quant.product_id
                        }
                        data['child_ids'].append((0, 0, child))
            IPVl.new(data)

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

    @api.depends('ipv_lines.request_qty', 'state')
    def _compute_show_check_availability(self):
        self.ensure_one()
        has_moves_to_reserve = any(float_compare(ipvl.request_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                   and ipvl.state not in ['assigned', 'done']
                                   for ipvl in self.ipv_lines
                                   )

        self.show_check_availability = self.is_locked and self.state in (
            'draft', 'check') and has_moves_to_reserve

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_validate(self):
        for ipv in self:
            if ipv.state not in ('check', 'assign', 'open'):
                ipv.show_validate = False
            else:
                ipv.show_validate = True

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_open(self):
        self.ensure_one()
        has_lines_to_sale = any(float_compare(ipvl.on_hand_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                   # and ipvl.state not in ['assigned', 'done']
                                   for ipvl in self.ipv_lines
                                )

        self.show_open = self.is_locked and self.state in (
                'draft', 'assign') and has_lines_to_sale

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
            return super(StockIpv, self).unlink()

    @api.multi
    def action_confirm(self):
        # call `_action_confirm` on every draft ipv line
        ipvl_todo = self.ipv_lines.action_confirm()
        return ipvl_todo

    @api.multi
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        ipvl_todo = self.filtered(lambda ipv: ipv.state == 'draft').action_confirm()
        ipvl_todo.action_assign()
        return True

    @api.multi
    def action_done(self):
        """"Changes picking state to done by processing the Stock Moves of the Picking

        Normally that happens when the button "Done" is pressed on a Picking view.
        @return: True
        """
        all_lines = self.ipv_lines | self.raw_lines
        all_lines.action_done()
        return True

    @api.multi
    def do_unreserve(self):
        for ipvl in self:
            ipvl.move_ids._do_unreserve()

    @api.multi
    def button_validate(self):
        self.ensure_one()
        all_lines = self.ipv_lines | self.raw_lines
        all_lines.action_validate()
        self.action_done()
        return True

    @api.multi
    def button_open(self):
        self.ensure_one()
        if self.show_validate:
            self.button_validate()
        self.write({'date_done': fields.Datetime.now()})
        return True

    @api.multi
    def button_close(self):
        return True


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'
    _parent_store = True

    ipv_id = fields.Many2one('stock.ipv', string='IPV Reference', ondelete='cascade')

    product_id = fields.Many2one('product.product', 'Product', required=True,
                                 domain=[('type', 'in', ['product']), ('available_in_pos', '=', True)],
                                 states={'assigned': [('readonly', True)],
                                         'done': [('readonly', True)]},
                                 )
    product_uom = fields.Many2one('uom.uom', related='product_id.uom_id', readonly=True)

    parent_id = fields.Many2one('stock.ipv.line', 'Parent/Product', ondelete='cascade')

    parent_path = fields.Char(index=True, readonly=True)

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
    is_locked = fields.Boolean('Is Locked?', compute='_compute_is_locked', readonly=True)

    is_manufactured = fields.Boolean('Is manufactured?', compute='_compute_is_manufactured', readonly=True, store=True)

    child_ids = fields.One2many('stock.ipv.line', 'parent_id', 'Raw Materials')

    has_moves = fields.Boolean('Has moves?', compute='_compute_has_moves')

    sublocation_id = fields.Many2one('stock.location',
                                     compute='_compute_sublocation',
                                     readonly=True,
                                     store=True
                                     )

    move_ids = fields.One2many(comodel_name='stock.move',
                               inverse_name='ipvl_id',
                               string='Moves')

    initial_stock_qty = fields.Float('Initial Stock', readonly=True)

    on_hand_qty = fields.Float('On Hand', compute='_compute_on_hand_qty', readonly=True)

    request_qty = fields.Float(string='Initial Demand')

    consumed_qty = fields.Float('Consumed', compute='_compute_consumed_qty')

    @api.model
    def name_get(self):
        result = []
        for ipvl in self:
            if ipvl.parent_id:
                name = "{}/{}".format(ipvl.parent_id.product_id.name, ipvl.product_id.name)
            else:
                name = "{}".format(ipvl.product_id.name)
            result.append((ipvl.id, name))
        return result

    @api.depends('product_id', 'ipv_id.location_dest_id')
    def _compute_sublocation(self):

        for ipvl in self:

            if ipvl.parent_id:
                ipvl.sublocation_id = ipvl.parent_id.sublocation_id
                continue
            elif ipvl.ipv_id:
                location_id = ipvl.ipv_id.location_dest_id

            else:
                location_id = self.env.ref('stock_ipv.ipv_location_destiny')

            ipvl.sublocation_id = self.env['stock.location'].search(['&', ('name', '=', ipvl.product_id.name),
                                                                     ('location_id', '=', location_id.id)])

    @api.depends('product_id')
    def _compute_is_manufactured(self):
        for ipvl in self:
            if ipvl.product_id.bom_count:
                ipvl.is_manufactured = True

    @api.onchange('product_id')
    def onchange_product_id(self):
        result = {}
        if not self.product_id:
            return result
        self.child_ids = []
        if self.is_manufactured:
            bom = self.env['mrp.bom']._bom_find(product=self.product_id)
            childs = []
            for boml in bom.bom_line_ids:
                data = {
                    # 'ipv_id': self.ipv_id.id,
                    # 'parent_id': self.id,
                    'product_id': boml.product_id.id,
                }
                childs.append((0, 0, data))
                self.update({'child_ids': childs})

    @api.depends('product_id')
    def _compute_on_hand_qty(self):
        """Computa la cantidad de productos a mano en tiempo real"""

        for ipvl in self:

            if ipvl.is_manufactured:
                bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)
                availability = []
                for boml in bom.bom_line_ids:
                    boml_qty = boml.product_qty
                    available_boml = boml.product_id.with_context({'location': ipvl.sublocation_id.id}).qty_available
                    available_qty = bom.product_qty * available_boml / boml_qty
                    available_qty_uom = bom.product_uom_id._compute_quantity(available_qty, ipvl.product_uom)
                    availability += [available_qty_uom]

                ipvl.on_hand_qty = min(availability)

            elif ipvl.sublocation_id:
                ipvl.on_hand_qty = ipvl.product_id.with_context(
                    {'location': ipvl.sublocation_id.id}).qty_available
            else:
                ipvl.on_hand_qty = 0.0

    @api.onchange('request_qty')
    def compute_request_qty_for_child(self):
        if not self.product_id:
            return {}

        if self.is_manufactured:
            bom = self.env['mrp.bom']._bom_find(product=self.product_id)

            # cantidad de veces que necesito la BoM
            factor = self.product_uom._compute_quantity(self.request_qty,
                                                        bom.product_uom_id) / bom.product_qty

            boms, lines = bom.explode(self.product_id, factor)

            for boml, line_data in lines:
                self.child_ids.filtered(lambda s: boml.product_id.id).update({'request_qty': line_data['qty']})
            print(self.child_ids.mapped(lambda s: (s.product_id.name, s.request_qty)))

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
                moves = ipvl.child_ids.mapped('move_ids')
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

    @api.constrains('parent_id')
    def _check_hierarchy(self):
        if not self._check_recursion():
            raise models.ValidationError(
                'Error! You cannot create recursive lines.')

    @api.model
    def create(self, vals):
        res = super().create(vals)
        return res

    @api.multi
    def action_confirm(self):
        """ Crea las sublocations las movidas de materias primas o de mercancias y las confirma """

        # Crear la localizacion para los productos nuevos
        self.filtered(lambda s: not s.sublocation_id)._set_location()

        # Generar las movidas de inventario para las mercancias y las materias primas
        ipvl_todo = self.mapped('child_ids') | self.filtered(lambda s: not s.is_manufactured)

        for ipvl in ipvl_todo:
            ipvl._generate_move()

        # Confirmar las Movidas
        ipvl_todo.mapped('move_ids').filtered(lambda move: move.state == 'draft')._action_confirm()

        return ipvl_todo

    def _set_location(self):
        for ipvl in self:

            ipvl.sublocation_id = self.env['stock.location'].create({
                'name': ipvl.product_id.name,
                'usage': 'production' if ipvl.is_manufactured else 'transit',
                'location_id': ipvl.ipv_id.location_dest_id.id,
            })
            if ipvl.is_manufactured:
                ipvl.child_ids.write({'sublocation_id': ipvl.sublocation_id.id})

    def _generate_move(self):
        self.ensure_one()
        ipvl = self
        # original_quantity = (self.product_qty - self.qty_produced) or 1.0
        data = {
            # 'sequence': bom_line.sequence,
            'name': ipvl.ipv_id.name or ipvl.parent_id.ipv_id.name,
            # 'bom_line_id': bom_line.id,
            # 'picking_type_id': self.picking_type_id.id,
            'product_id': ipvl.product_id.id,
            'product_uom_qty': ipvl.request_qty,
            'product_uom': ipvl.product_uom.id,
            'location_id': ipvl.ipv_id.location_id.id or ipvl.parent_id.ipv_id.location_id.id,
            'location_dest_id': ipvl.sublocation_id.id,
            # 'raw_material_production_id': self.id,
            # 'company_id': self.company_id.id,
            # 'operation_id': bom_line.operation_id.id or alt_op,
            # 'price_unit': bom_line.product_id.standard_price,
            # 'procure_method': 'make_to_stock',
            # 'origin': self.name,
            'warehouse_id': ipvl.ipv_id.location_id.get_warehouse().id or ipvl.parent_id.ipv_id.location_id.get_warehouse().id,
            # 'group_id': self.procurement_group_id.id,
            # 'propagate': self.propagate,
            # 'unit_factor': quantity / original_quantity,
        }
        ipvl.move_ids = self.env['stock.move'].create(data)

    @api.multi
    def action_assign(self):

        moves_to_check = self.mapped('move_ids').filtered(lambda move: move.state not in ('draft', 'cancel', 'done'))

        if not moves_to_check:
            raise UserError('Nothing to check the availability for.')
        moves_to_check._action_assign()

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
