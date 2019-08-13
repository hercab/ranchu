# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.tools.float_utils import float_compare, float_is_zero, float_round
from odoo.exceptions import UserError


class StockIpv(models.Model):
    _name = 'stock.ipv'
    _description = 'Stock IPV'
    _order = 'create_date desc'

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

    picking_id = fields.Many2one('stock.picking')

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

    raw_lines = fields.One2many('stock.ipv.line', compute='compute_raw_lines')

    move_lines = fields.One2many('stock.move', compute='_compute_move_lines', string='Move Lines')

    show_check_availability = fields.Boolean(
        compute='_compute_show_check_availability',
        help='Technical field used to compute whether the check availability button should be shown.')

    show_open = fields.Boolean(
        compute='_compute_show_open',
        help='Technical field used to compute whether the Open button should be shown.')
    is_locked = fields.Boolean(default=True, help='When the picking is not done this allows changing the '
                                                  'initial demand. When the picking is done this allows '
                                                  'changing the done quantities.')

    date_open = fields.Datetime('Open date', copy=False, readonly=True,
                                help="Date at which the turn has been processed or cancelled.")

    date_close = fields.Datetime('Close date', copy=False, readonly=True,
                                 help="Date at which the turn was closed.")

    @api.depends('ipv_lines.raw_ids')
    def compute_raw_lines(self):
        self.ensure_one()
        if not self.ipv_lines:
            return {}
        self.raw_lines = self.ipv_lines.mapped('raw_ids')

    @api.depends('ipv_lines.move_ids', 'raw_lines.move_ids')
    def _compute_move_lines(self):
        self.move_lines = (self.ipv_lines + self.raw_lines).mapped('move_ids')

    @api.onchange('location_dest_id')
    def _compute_child_lines(self):
        dest = self.location_dest_id
        IPVl = self.env['stock.ipv.line']
        # Quant = self.env['stock.quant']
        sublocations = dest.child_ids
        self.ipv_lines = []
        ipvls = []
        for sublocation in sublocations:
            product = self.env['product.product'].search([('name', '=', sublocation.name)], limit=1)

            if sublocation.quant_ids:
                data = {
                    'product_id': product.id,
                    'raw_ids': []
                }

                if sublocation.usage == 'production':
                    for quant in sublocation.quant_ids:
                        raw = {
                            'product_id': quant.product_id.id
                        }
                        data['raw_ids'].append((0, 0, raw))
                ipvls.append((0, 0, data))
        self.update({'ipv_lines': ipvls})

    @api.depends('picking_id.state')
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

        if not self.picking_id:
            self.state = 'draft'
        elif self.picking_id.state == 'draft':
            self.state = 'draft'
        elif self.picking_id.state in ['assigned']:
            self.state = 'assign'
        elif self.picking_id.state in ['done']:
            self.state = 'open'
        else:
            self.state = 'check'

    @api.depends('ipv_lines.request_qty', 'state')
    def _compute_show_check_availability(self):
        self.ensure_one()
        has_moves_to_reserve = any(float_compare(ipvl.request_qty, 0, precision_rounding=ipvl.product_uom.rounding)
                                   and ipvl.state not in ['assigned', 'done']
                                   for ipvl in self.ipv_lines
                                   )

        self.show_check_availability = self.is_locked and self.state not in (
            'close') and has_moves_to_reserve

    @api.multi
    @api.depends('state', 'is_locked')
    def _compute_show_open(self):
        self.ensure_one()
        self.show_open = self.is_locked and (self.state in 'assign')

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

    @api.one
    def action_confirm(self):
        # call `_action_confirm` on every draft ipv line
        ipvl_confirmed = self.ipv_lines.filtered(lambda ipvl: ipvl.state == 'draft').action_confirm()

        if ipvl_confirmed:
            self.picking_id = ipvl_confirmed.mapped('move_ids.picking_id')
        return True

    @api.one
    def action_assign(self):
        """ Check availability of picking moves.
        This has the effect of changing the state and reserve quants on available moves, and may
        also impact the state of the picking as it is computed based on move's states.
        @return: True
        """
        self.filtered(lambda ipv: ipv.state == 'draft').action_confirm()

        # Cuando se confirma las movidas se crean los picking asociados
        self.picking_id.action_assign()
        return True

    @api.one
    def button_open(self):
        for move in self.move_lines:
            if move.is_quantity_done_editable:
                move.quantity_done = move.product_uom_qty
            else:
                for ml in move.move_line_ids:
                    ml.qty_done = ml.product_uom_qty

        self.picking_id.button_validate()
        self.write({
            'state': 'open',
            'date_open': fields.Datetime.now()})
        return True

    @api.multi
    def button_close(self):
        return True


class StockIpvLine(models.Model):
    _name = 'stock.ipv.line'
    _description = 'Ipv Line'

    ipv_id = fields.Many2one('stock.ipv', string='IPV Reference', ondelete='cascade')

    product_id = fields.Many2one('product.product', 'Product', required=True,
                                 domain=[('type', 'in', ['product']), ('available_in_pos', '=', True)],
                                 )
    product_uom = fields.Many2one('uom.uom', related='product_id.uom_id', readonly=True)

    manufactured_id = fields.Many2one('stock.ipv.line', 'Manufactured Product',
                                      help='Product that is manufactured')
    raw_ids = fields.One2many('stock.ipv.line', 'manufactured_id', 'Raw Materials',
                              help='Optional: Raw Materials for this Product',
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
    is_locked = fields.Boolean('Is Locked?', compute='_compute_is_locked', readonly=True)

    is_manufactured = fields.Boolean('Is manufactured?', compute='_compute_is_manufactured', readonly=True)

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

    on_hand_qty = fields.Float('On Hand', compute='_compute_on_hand_qty', readonly=True,
                               help='Cantidad a mano en el area de venta, puede entrar la cantidad que desea tener')

    request_qty = fields.Float(string='Initial Demand', help='Cantidad que desea mover al area de venta')

    consumed_qty = fields.Float('Consumed', compute='_compute_consumed_qty')

    @api.model
    def name_get(self):
        result = []
        for ipvl in self:
            if ipvl.manufactured_id:
                name = "{}/{}".format(ipvl.manufactured_id.product_id.name, ipvl.product_id.name)
            else:
                name = "{}".format(ipvl.product_id.name)
            result.append((ipvl.id, name))
        return result

    @api.depends('product_id', 'ipv_id.location_dest_id')
    def _compute_sublocation(self):

        for ipvl in self:

            if ipvl.manufactured_id:
                ipvl.sublocation_id = ipvl.manufactured_id.sublocation_id
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
    def compute_raw_ids(self):
        ipvl = self
        # self.child_ids = []
        if ipvl.is_manufactured:
            bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)
            childs = []
            for boml in bom.bom_line_ids:
                data = {
                    'product_id': boml.product_id.id,
                }
                childs.append((0, 0, data))
            self.update({'raw_ids': childs})

    @api.depends('product_id')
    def _compute_on_hand_qty(self):
        """Computa la cantidad de productos a mano en el area de venta"""

        for ipvl in self:

            if ipvl.is_manufactured and ipvl.sublocation_id.quant_ids:
                bom = self.env['mrp.bom']._bom_find(product=ipvl.product_id)
                availability = []
                for boml in bom.bom_line_ids:
                    boml_qty = boml.product_qty
                    available_boml = boml.product_id.with_context({'location': ipvl.sublocation_id.id}).qty_available
                    available_qty = bom.product_qty * available_boml / boml_qty
                    available_qty_uom = bom.product_uom_id._compute_quantity(available_qty, ipvl.product_uom)
                    availability += [available_qty_uom]

                ipvl.on_hand_qty = min(availability)

            elif ipvl.sublocation_id.quant_ids:
                ipvl.on_hand_qty = ipvl.product_id.with_context(
                    {'location': ipvl.sublocation_id.id}).qty_available

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
                self.raw_ids.filtered(lambda raw: raw.product_id.id == boml.product_id.id).update({'request_qty': line_data['qty']})

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
        # Valorar si crear aqui los raw_ids si es manufacturado
        res = super(StockIpvLine, self).create(vals)
        return res

    @api.multi
    def action_confirm(self):
        """ Crea las sublocations las movidas de materias primas o de mercancias y las confirma """

        # Crear la localizacion para los productos nuevos
        self.filtered(lambda s: not s.sublocation_id)._set_location()

        # Generar las movidas de inventario para las mercancias y las materias primas
        ipvl_todo = self.mapped('raw_ids') + self.filtered(lambda ipvl: not ipvl.is_manufactured)

        ipvl_todo._generate_moves()

        # Confirmar las Movidas
        ipvl_todo.mapped('move_ids').filtered(lambda move: move.state == 'draft')._action_confirm()

        return ipvl_todo

    def _set_location(self):
        if not self:
            return {}
        puts = []
        for ipvl in self:

            ipvl.sublocation_id = self.env['stock.location'].create({
                'name': ipvl.product_id.name,
                'usage': 'production' if ipvl.is_manufactured else 'transit',
                'location_id': ipvl.ipv_id.location_dest_id.id,
            })
            puts.append((0, 0, {'product_id': ipvl.product_id.id, 'fixed_location_id': ipvl.sublocation_id.id}))
            if ipvl.is_manufactured:
                ipvl.raw_ids.write({'sublocation_id': ipvl.sublocation_id.id})
                for raw in ipvl.raw_ids:
                    puts.append((0, 0, {'product_id': raw.product_id.id, 'fixed_location_id': raw.sublocation_id.id}))

        # Crear Putaway ahora con los productos nuevos para este destino
        self[0].ipv_id.location_dest_id.putaway_strategy_id.write({'product_location_ids': puts})
        return True

    def _generate_moves(self):
        for ipvl in self:
            # original_quantity = (self.product_qty - self.qty_produced) or 1.0
            data = {
                # 'sequence': bom_line.sequence,
                'name': ipvl.manufactured_id.ipv_id.name or ipvl.ipv_id.name,
                # 'bom_line_id': bom_line.id,
                'picking_type_id': self.env['stock.picking.type'].search([('code', '=', 'internal')]).id,
                'product_id': ipvl.product_id.id,
                'product_uom_qty': ipvl.request_qty,
                'product_uom': ipvl.product_uom.id,
                'location_id': ipvl.manufactured_id.ipv_id.location_id.id or ipvl.ipv_id.location_id.id,
                'location_dest_id': ipvl.manufactured_id.ipv_id.location_dest_id.id or ipvl.ipv_id.location_dest_id.id,
                # 'raw_material_production_id': self.id,
                # 'company_id': self.company_id.id,
                # 'operation_id': bom_line.operation_id.id or alt_op,
                # 'price_unit': bom_line.product_id.standard_price,
                # 'procure_method': 'make_to_stock',
                'origin': ipvl.manufactured_id.ipv_id.name or ipvl.ipv_id.name,
                'warehouse_id': ipvl.manufactured_id.ipv_id.location_id.get_warehouse().id or ipvl.ipv_id.location_id.get_warehouse().id,
                # 'group_id': self.procurement_group_id.id,
                # 'propagate': self.propagate,
                # 'unit_factor': quantity / original_quantity,
            }
            ipvl.move_ids = self.env['stock.move'].create(data)
